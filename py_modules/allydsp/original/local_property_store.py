"""Process-local Windows IPropertyStore for unmodified APO configuration paths.

Returned blobs use CoTaskMemAlloc: the vendor caller owns PropVariantClear.
No COM registration, Windows endpoint, registry or system service is touched.
"""
import ctypes as C
import struct
import uuid

HRESULT = C.c_int32
DWORD = C.c_uint32
CALL = C.WINFUNCTYPE
E_NOT_FOUND = C.c_int32(0x80070490).value
E_NOINTERFACE = C.c_int32(0x80004002).value
E_NOTIMPL = C.c_int32(0x80004001).value


def key_bytes(fmtid, pid):
    return uuid.UUID(fmtid).bytes_le + struct.pack('<I', pid)


def key_text(raw):
    return str(uuid.UUID(bytes_le=raw[:16])) + ':' + str(struct.unpack('<I', raw[16:20])[0])


class LocalPropertyStore:
    def __init__(self, values):
        self.values = dict(values)
        self.audit = []
        self.errors = []
        self.refs = 1
        self.ole32 = C.WinDLL('ole32.dll')
        self.ole32.CoTaskMemAlloc.argtypes = [C.c_size_t]
        self.ole32.CoTaskMemAlloc.restype = C.c_void_p
        self._callbacks = [
            CALL(HRESULT, C.c_void_p, C.c_void_p, C.c_void_p)(self._query),
            CALL(DWORD, C.c_void_p)(self._addref),
            CALL(DWORD, C.c_void_p)(self._release),
            CALL(HRESULT, C.c_void_p, C.c_void_p)(self._count),
            CALL(HRESULT, C.c_void_p, DWORD, C.c_void_p)(self._at),
            CALL(HRESULT, C.c_void_p, C.c_void_p, C.c_void_p)(self._get),
            CALL(HRESULT, C.c_void_p, C.c_void_p, C.c_void_p)(lambda *_: E_NOTIMPL),
            CALL(HRESULT, C.c_void_p)(lambda *_: 0),
        ]
        self._vtable = (C.c_void_p * len(self._callbacks))(
            *(C.cast(callback, C.c_void_p).value for callback in self._callbacks))
        self._object = (C.c_void_p * 1)(C.addressof(self._vtable))
        self.pointer = C.addressof(self._object)

    def _query(self, this, iid, output):
        requested = C.string_at(iid, 16)
        known = (uuid.UUID('00000000-0000-0000-c000-000000000046').bytes_le,
                 uuid.UUID('886d8eeb-8cf2-4446-8d02-cdba1dbdcf99').bytes_le)
        C.c_void_p.from_address(output).value = self.pointer if requested in known else None
        if requested in known:
            self.refs += 1
            return 0
        return E_NOINTERFACE

    def _addref(self, _):
        self.refs += 1
        return self.refs

    def _release(self, _):
        self.refs = max(0, self.refs - 1)
        return self.refs

    def _count(self, _, output):
        C.c_uint32.from_address(output).value = len(self.values)
        return 0

    def _at(self, _, index, output):
        if index >= len(self.values):
            return E_NOT_FOUND
        C.memmove(output, list(self.values)[index], 20)
        return 0

    def _get(self, _, key_pointer, variant):
        raw = C.string_at(key_pointer, 20)
        record = {'key': key_text(raw), 'supplied': raw in self.values}
        self.audit.append(record)
        C.memset(variant, 0, 24)
        if raw not in self.values:
            return E_NOT_FOUND
        try:
            spec = self.values[raw]
            kind, value = spec['kind'], spec['value']
            record.update(name=spec.get('name'), kind=kind)
            if kind in ('i4', 'ui4'):
                C.c_uint16.from_address(variant).value = 3 if kind == 'i4' else 19
                C.c_int32.from_address(variant + 8).value = int(value)
                record['value'] = int(value)
            elif kind == 'blob_i32':
                payload = struct.pack('<%di' % len(value), *value)
                allocated = self.ole32.CoTaskMemAlloc(len(payload))
                if not allocated:
                    return C.c_int32(0x8007000e).value
                C.memmove(allocated, payload, len(payload))
                C.c_uint16.from_address(variant).value = 65
                C.c_uint32.from_address(variant + 8).value = len(payload)
                C.c_void_p.from_address(variant + 16).value = allocated
                record['value'] = list(value)
            else:
                raise ValueError('Unsupported property kind: ' + kind)
            return 0
        except Exception as error:
            self.errors.append(str(error))
            return E_NOTIMPL
