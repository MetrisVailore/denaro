import random
from asyncio import gather
from collections import deque
from os import environ
import re

from asyncpg import UniqueViolationError
from fastapi import FastAPI, Body, Query
from fastapi.responses import RedirectResponse

from httpx import TimeoutException
from icecream import ic
from starlette.background import BackgroundTasks
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from denaro.helpers import timestamp, sha256, transaction_to_json
from denaro.manager import create_block, get_difficulty, Manager, get_transactions_merkle_tree, \
    split_block_content, calculate_difficulty, clear_pending_transactions, block_to_bytes, get_transactions_merkle_tree_ordered
from denaro.node.nodes_manager import NodesManager, NodeInterface
from denaro.node.utils import ip_is_local
from denaro.transactions import Transaction, CoinbaseTransaction
from denaro import Database
from denaro.constants import VERSION, ENDIAN


limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
db: Database = None
NodesManager.init()
started = False
is_syncing = False
self_url = None

print = ic

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


async def propagate(path: str, args: dict, ignore_url=None, nodes: list = None):
    global self_url
    self_node = NodeInterface(self_url or '')
    ignore_node = NodeInterface(ignore_url or '')
    aws = []
    for node_url in nodes or NodesManager.get_propagate_nodes():
        node_interface = NodeInterface(node_url)
        if node_interface.base_url == self_node.base_url or node_interface.base_url == ignore_node.base_url:
            continue
        aws.append(node_interface.request(path, args, self_node.url))
    for response in await gather(*aws, return_exceptions=True):
        print('node response: ', response)


async def create_blocks(blocks: list):
    _, last_block = await calculate_difficulty()
    last_block['id'] = last_block['id'] if last_block != {} else 0
    last_block['hash'] = last_block['hash'] if 'hash' in last_block else (30_06_2005).to_bytes(32, ENDIAN).hex()
    i = last_block['id'] + 1
    for block_info in blocks:
        block = block_info['block']
        txs_hex = block_info['transactions']
        txs = [await Transaction.from_hex(tx) for tx in txs_hex]
        for tx in txs:
            if isinstance(tx, CoinbaseTransaction):
                txs.remove(tx)
                break
        hex_txs = [tx.hex() for tx in txs]
        block['merkle_tree'] = get_transactions_merkle_tree(hex_txs) if i > 22500 else get_transactions_merkle_tree_ordered(hex_txs)
        block_content = block.get('content') or block_to_bytes(last_block['hash'], block)

        if i <= 22500 and sha256(block_content) != block['hash'] and i != 17972:
            from itertools import permutations
            for l in permutations(hex_txs):
                _hex_txs = list(l)
                block['merkle_tree'] = get_transactions_merkle_tree_ordered(_hex_txs)
                block_content = block_to_bytes(last_block['hash'], block)
                if sha256(block_content) == block['hash']:
                    break
        elif 131309 < i < 150000 and sha256(block_content) != block['hash']:
            for diff in range(0, 100):
                block['difficulty'] = diff / 10
                block_content = block_to_bytes(last_block['hash'], block)
                if sha256(block_content) == block['hash']:
                    break
        assert i == block['id']
        if not await create_block(block_content.hex() if isinstance(block_content, bytes) else block_content, txs, last_block):
            return False
        last_block = block
        i += 1
    return True


async def _sync_blockchain(node_url: str = None):
    print('sync blockchain')
    if not node_url:
        nodes = NodesManager.get_recent_nodes()
        if not nodes:
            return
        node_url = random.choice(nodes)
    node_url = node_url.strip('/')
    _, last_block = await calculate_difficulty()
    starting_from = i = await db.get_next_block_id()
    node_interface = NodeInterface(node_url)
    local_cache = None
    if last_block != {} and last_block['id'] > 500:
        remote_last_block = (await node_interface.get_block(i-1))['block']
        if remote_last_block['hash'] != last_block['hash']:
            print(remote_last_block['hash'])
            offset, limit = i - 500, 500
            remote_blocks = await node_interface.get_blocks(offset, limit)
            local_blocks = await db.get_blocks(offset, limit)
            local_blocks = local_blocks[:len(remote_blocks)]
            local_blocks.reverse()
            remote_blocks.reverse()
            print(len(remote_blocks), len(local_blocks))
            for n, local_block in enumerate(local_blocks):
                if local_block['block']['hash'] == remote_blocks[n]['block']['hash']:
                    print(local_block, remote_blocks[n])
                    last_common_block = local_block['block']['id']
                    local_cache = local_blocks[:n]
                    local_cache.reverse()
                    await db.remove_blocks(last_common_block + 1)
                    break

    limit = 1000
    while True:
        i = await db.get_next_block_id()
        try:
            blocks = await node_interface.get_blocks(i, limit)
        except Exception as e:
            print(e)
            NodesManager.sync()
            break
        try:
            _, last_block = await calculate_difficulty()
            if not blocks:
                print('syncing complete')
                if last_block['id'] > starting_from:
                    NodesManager.update_last_message(node_url)
                    if timestamp() - last_block['timestamp'] < 86400:
                        txs_hashes = await db.get_block_transaction_hashes(last_block['hash'])
                        await propagate('push_block', {'block_content': last_block['content'], 'txs': txs_hashes, 'block_no': last_block['id']}, node_url)
                break
            assert await create_blocks(blocks)
        except Exception as e:
            print(e)
            if local_cache is not None:
                print('sync failed, reverting back to previous chain')
                await db.delete_blocks(last_common_block)
                await create_blocks(local_cache)
            return


async def sync_blockchain(node_url: str = None):
    try:
        await _sync_blockchain(node_url)
    except Exception as e:
        print(e)
        return


@app.on_event("startup")
async def startup():
    global db
    db = await Database.create(
        user=environ.get('DENARO_DATABASE_USER', 'denaro'),
        password=environ.get('DENARO_DATABASE_PASSWORD', ''),
        database=environ.get('DENARO_DATABASE_NAME', 'denaro'),
        host=environ.get('DENARO_DATABASE_HOST', None)
    )


@app.get("/")
async def root():
    return {"version": VERSION, "unspent_outputs_hash": await db.get_unspent_outputs_hash()}


async def propagate_old_transactions(propagate_txs):
    await db.update_pending_transactions_propagation_time([sha256(tx_hex) for tx_hex in propagate_txs])
    for tx_hex in propagate_txs:
        await propagate('push_tx', {'tx_hex': tx_hex})


@app.middleware("http")
async def middleware(request: Request, call_next):
    global started, self_url
    nodes = NodesManager.get_recent_nodes()
    hostname = request.base_url.hostname

    # Normalize the URL path by removing extra slashes
    normalized_path = re.sub('/+', '/', request.scope['path'])
    if normalized_path != request.scope['path']:
        url = request.url
        new_url = str(url).replace(request.scope['path'], normalized_path)
        #Redirect to normalized URL
        return RedirectResponse(new_url)

    if 'Sender-Node' in request.headers:
        NodesManager.add_node(request.headers['Sender-Node'])

    if nodes and not started or (ip_is_local(hostname) or hostname == 'localhost'):
        try:
            node_url = nodes[0]
            j = await NodesManager.request(f'{node_url}/get_nodes')
            nodes.extend(j['result'])
            NodesManager.sync()
        except:
            pass

        if not (ip_is_local(hostname) or hostname == 'localhost'):
            started = True

            self_url = str(request.base_url).strip('/')
            try:
                nodes.remove(self_url)
            except ValueError:
                pass
            try:
                nodes.remove(self_url.replace("http://", "https://"))
            except ValueError:
                pass

            NodesManager.sync()

            try:
                await propagate('add_node', {'url': self_url})
                cousin_nodes = sum(await NodeInterface(url).get_nodes() for url in nodes)
                await propagate('add_node', {'url': self_url}, nodes=cousin_nodes)
            except:
                pass
    propagate_txs = await db.get_need_propagate_transactions()
    try:
        response = await call_next(request)
        response.headers['Access-Control-Allow-Origin'] = '*'
        if propagate_txs:
            response.background = BackgroundTask(propagate_old_transactions, propagate_txs)
        return response
    except:
        raise
        return {'ok': False, 'error': 'Internal error'}


@app.exception_handler(Exception)
async def exception_handler(request: Request, e: Exception):
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": f"Uncaught {type(e).__name__} exception"},
    )

transactions_cache = deque(maxlen=100)


@app.get("/push_tx")
@app.post("/push_tx")
async def push_tx(request: Request, background_tasks: BackgroundTasks, tx_hex: str = None, body=Body(False)):
    if body and tx_hex is None:
        tx_hex = body['tx_hex']
    tx = await Transaction.from_hex(tx_hex)
    if tx.hash() in transactions_cache:
        return {'ok': False, 'error': 'Transaction just added'}
    try:
        if await db.add_pending_transaction(tx):
            if 'Sender-Node' in request.headers:
                NodesManager.update_last_message(request.headers['Sender-Node'])
            # Handle background task for transaction propagation
            background_tasks.add_task(propagate_old_transactions, [tx_hex])
            return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
