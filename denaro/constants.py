from fastecdsa import curve
from decimal import Decimal

ENDIAN = 'little'
CURVE = curve.P256
SMALLEST = 1000000
MAX_SUPPLY = 30_062_005
BLOCK_TIME = 30
BLOCKS_DIFF_CHANGE = Decimal(120)
START_DIFF = Decimal('3.0')
VERSION = 1
MAX_BLOCK_SIZE_HEX = 4096 * 1024  # 4MB in HEX format, 2MB in raw bytes
DEV_NODE = "https://"
