import json
import mmap
import os
import struct

_INITIAL_MMAP_SIZE = 1 << 20
_pack_integer_func = struct.Struct(b'i').pack
_value_timestamp = struct.Struct(b'dd')
_unpack_integer = struct.Struct(b'i').unpack_from


# struct.pack_into has atomicity issues because it will temporarily write 0 into
# the mmap, resulting in false reads to 0 when experiencing a lot of writes.
# Using direct assignment solves this issue.
def _pack_value_timestamp(data, pos, value, timestamp):
    data[pos:pos + _value_timestamp.size] = _value_timestamp.pack(value, timestamp)


def _pack_integer(data, pos, value):
    data[pos:pos + 4] = _pack_integer_func(value)


class MmapedDict(object):
    """A dict of doubles, backed by an mmapped file.

    The file starts with a 4 byte int, indicating how much of it is used.
    Then 4 bytes of padding.
    There's then a number of entries, consisting of a 4 byte int which is the
    size of the next field, a utf-8 encoded string key, padding to a 8 byte
    alignment, a 8 byte float which is the value and then an 8 byte timestamp (seconds).

    Not thread safe.
    """

    def __init__(self, filename, read_mode=False):
        self._f = open(filename, 'rb' if read_mode else 'a+b')
        self._fname = filename
        if os.fstat(self._f.fileno()).st_size == 0:
            self._f.truncate(_INITIAL_MMAP_SIZE)
        self._capacity = os.fstat(self._f.fileno()).st_size
        self._m = mmap.mmap(self._f.fileno(), self._capacity,
                            access=mmap.ACCESS_READ if read_mode else mmap.ACCESS_WRITE)

        self._positions = {}
        self._used = _unpack_integer(self._m, 0)[0]
        if self._used == 0:
            self._used = 8
            _pack_integer(self._m, 0, self._used)
        else:
            if not read_mode:
                for key, _, _, pos in self._read_all_values():
                    self._positions[key] = pos

    def _init_value(self, key):
        """Initialize a value. Lock must be held by caller."""
        encoded = key.encode('utf-8')
        # Pad to be 8-byte aligned.
        padded = encoded + (b' ' * (8 - (len(encoded) + 4) % 8))
        value = struct.pack('i{0}sdd'.format(len(padded)).encode(), len(encoded), padded, 0.0, 0.0)
        while self._used + len(value) > self._capacity:
            self._capacity *= 2
            self._f.truncate(self._capacity)
            self._m = mmap.mmap(self._f.fileno(), self._capacity)
        self._m[self._used:self._used + len(value)] = value

        # Update how much space we've used.
        self._used += len(value)
        _pack_integer(self._m, 0, self._used)
        self._positions[key] = self._used - _value_timestamp.size

    def _read_all_values(self):
        """Yield (key, value, timestamp, pos). No locking is performed."""

        pos = 8

        # cache variables to local ones and prevent attributes lookup
        # on every loop iteration
        used = self._used
        data = self._m
        unpack_from = struct.unpack_from

        while pos < used:
            encoded_len = _unpack_integer(data, pos)[0]
            # check we are not reading beyond bounds
            if encoded_len + pos > used:
                msg = 'Read beyond file size detected, %s is corrupted.'
                raise RuntimeError(msg % self._fname)
            pos += 4
            encoded = unpack_from(('%ss' % encoded_len).encode(), data, pos)[0]
            padded_len = encoded_len + (8 - (encoded_len + 4) % 8)
            pos += padded_len
            value, timestamp = _value_timestamp.unpack_from(data, pos)
            yield encoded.decode('utf-8'), value, _from_timestamp_float(timestamp), pos
            pos += _value_timestamp.size

    def read_all_values(self):
        """Yield (key, value, pos). No locking is performed."""
        for k, v, ts, _ in self._read_all_values():
            yield k, v, ts

    def read_value_timestamp(self, key):
        if key not in self._positions:
            self._init_value(key)
        pos = self._positions[key]
        # We assume that reading from an 8 byte aligned value is atomic
        val, ts = _value_timestamp.unpack_from(self._m, pos)
        return val, _from_timestamp_float(ts)

    def read_value(self, key):
        return self.read_value_timestamp(key)[0]

    def write_value(self, key, value, timestamp=None):
        if key not in self._positions:
            self._init_value(key)
        pos = self._positions[key]
        # We assume that writing to an 8 byte aligned value is atomic
        _pack_value_timestamp(self._m, pos, value, _to_timestamp_float(timestamp))

    def close(self):
        if self._f:
            self._m.close()
            self._m = None
            self._f.close()
            self._f = None


def mmap_key(metric_name, name, labelnames, labelvalues):
    """Format a key for use in the mmap file."""
    # ensure labels are in consistent order for identity
    labels = dict(zip(labelnames, labelvalues))
    return json.dumps([metric_name, name, labels], sort_keys=True)


def _from_timestamp_float(timestamp):
    """Convert timestamp from a pure floating point value

    inf is decoded as None
    """
    if timestamp == float('inf'):
        return None
    else:
        return timestamp


def _to_timestamp_float(timestamp):
    """Convert timestamp to a pure floating point value

    None is encoded as inf
    """
    if timestamp is None:
        return float('inf')
    else:
        return float(timestamp)
