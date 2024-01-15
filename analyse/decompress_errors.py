"""
Create some data, compress it, randomly corrupt it, and then decompress it.
"""
from collections import defaultdict
from random import Random

from zstandard import ZstdCompressor, ZstdDecompressor

random = Random(1)


def test_compress_decompress():
    error_type_counts = defaultdict(int)
    for i in range(10000):
        compressor = ZstdCompressor()
        decompressor = ZstdDecompressor()
        random_string = (''.join(random.choice('abcdefghijklmnopqrstuvwxyz') for _ in range(4096))).encode('utf8')
        compressed_data = compressor.compress(random_string)

        # Corrupt the data
        compressed_data_array = bytearray(compressed_data)

        # Corrupt some random bytes
        for _ in range(10):
            compressed_data_array[random.randint(0, len(compressed_data_array) - 1)] = random.randint(0, 255)

        corrupted_data = bytes(compressed_data_array)

        try:
            decompressed_data = decompressor.decompress(corrupted_data)
        except Exception as e:
            error_type_counts[f"{type(e)}: {str(e)}"] += 1
            continue
        error_type_counts["none"] += 1
    print("Counts")
    for error_type, count in error_type_counts.items():
        print(f"{error_type}: {count}")
