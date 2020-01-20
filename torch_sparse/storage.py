import warnings

import torch
from torch_scatter import segment_csr, scatter_add

from torch_sparse import rowptr_cpu

try:
    from torch_sparse import rowptr_cuda
except ImportError:
    rowptr_cuda = None

__cache__ = {'enabled': True}


def is_cache_enabled():
    return __cache__['enabled']


def set_cache_enabled(mode):
    __cache__['enabled'] = mode


class no_cache(object):
    def __enter__(self):
        self.prev = is_cache_enabled()
        set_cache_enabled(False)

    def __exit__(self, *args):
        set_cache_enabled(self.prev)
        return False

    def __call__(self, func):
        def decorate_no_cache(*args, **kwargs):
            with self:
                return func(*args, **kwargs)

        return decorate_no_cache


class cached_property(object):
    def __init__(self, func):
        self.func = func

    def __get__(self, obj, cls):
        value = getattr(obj, f'_{self.func.__name__}', None)
        if value is None:
            value = self.func(obj)
            if is_cache_enabled():
                setattr(obj, f'_{self.func.__name__}', value)
        return value


def optional(func, src):
    return func(src) if src is not None else src


layouts = ['coo', 'csr', 'csc']


def get_layout(layout=None):
    if layout is None:
        layout = 'coo'
        warnings.warn('`layout` argument unset, using default layout '
                      '"coo". This may lead to unexpected behaviour.')
    assert layout in layouts
    return layout


class SparseStorage(object):
    cache_keys = [
        'rowcount', 'rowptr', 'colcount', 'colptr', 'csr2csc', 'csc2csr'
    ]

    def __init__(self, index, value=None, sparse_size=None, rowcount=None,
                 rowptr=None, colcount=None, colptr=None, csr2csc=None,
                 csc2csr=None, is_sorted=False):

        assert index.dtype == torch.long
        assert index.dim() == 2 and index.size(0) == 2
        index = index.contiguous()

        if value is not None:
            assert value.device == index.device
            assert value.size(0) == index.size(1)
            value = value.contiguous()

        if sparse_size is None:
            sparse_size = torch.Size((index.max(dim=-1)[0] + 1).tolist())

        if rowcount is not None:
            assert rowcount.dtype == torch.long
            assert rowcount.device == index.device
            assert rowcount.dim() == 1 and rowcount.numel() == sparse_size[0]

        if rowptr is not None:
            assert rowptr.dtype == torch.long
            assert rowptr.device == index.device
            assert rowptr.dim() == 1 and rowptr.numel() - 1 == sparse_size[0]

        if colcount is not None:
            assert colcount.dtype == torch.long
            assert colcount.device == index.device
            assert colcount.dim() == 1 and colcount.numel() == sparse_size[1]

        if colptr is not None:
            assert colptr.dtype == torch.long
            assert colptr.device == index.device
            assert colptr.dim() == 1 and colptr.numel() - 1 == sparse_size[1]

        if csr2csc is not None:
            assert csr2csc.dtype == torch.long
            assert csr2csc.device == index.device
            assert csr2csc.dim() == 1
            assert csr2csc.numel() == index.size(1)

        if csc2csr is not None:
            assert csc2csr.dtype == torch.long
            assert csc2csr.device == index.device
            assert csc2csr.dim() == 1
            assert csc2csr.numel() == index.size(1)

        if not is_sorted:
            idx = sparse_size[1] * index[0] + index[1]
            # Only sort if necessary...
            if (idx < torch.cat([idx.new_zeros(1), idx[:-1]], dim=0)).any():
                perm = idx.argsort()
                index = index[:, perm]
                value = None if value is None else value[perm]
                csr2csc = None
                csc2csr = None

        self._index = index
        self._value = value
        self._sparse_size = sparse_size
        self._rowcount = rowcount
        self._rowptr = rowptr
        self._colcount = colcount
        self._colptr = colptr
        self._csr2csc = csr2csc
        self._csc2csr = csc2csr

    @property
    def index(self):
        return self._index

    @property
    def row(self):
        return self._index[0]

    @property
    def col(self):
        return self._index[1]

    def has_value(self):
        return self._value is not None

    @property
    def value(self):
        return self._value

    def set_value_(self, value, layout=None):
        if isinstance(value, int) or isinstance(value, float):
            value = torch.full((self.nnz(), ), device=self.index.device)
        elif torch.is_tensor(value) and get_layout(layout) == 'csc':
            value = value[self.csc2csr]
        assert value.device == self.index.device
        assert value.size(0) == self.index.size(1)
        self._value = value
        return self

    def set_value(self, value, layout=None):
        if isinstance(value, int) or isinstance(value, float):
            value = torch.full((self.nnz(), ), device=self.index.device)
        elif torch.is_tensor(value) and get_layout(layout) == 'csc':
            value = value[self.csc2csr]
        assert value.device == self._index.device
        assert value.size(0) == self._index.size(1)
        return self.__class__(
            self._index,
            value,
            self._sparse_size,
            self._rowcount,
            self._rowptr,
            self._colcount,
            self._colptr,
            self._csr2csc,
            self._csc2csr,
            is_sorted=True,
        )

    def sparse_size(self, dim=None):
        return self._sparse_size if dim is None else self._sparse_size[dim]

    def sparse_resize(self, *sizes):
        assert len(sizes) == 2
        old_sizes, nnz = self.sparse_size(), self.nnz()

        diff_0 = sizes[0] - old_sizes[0]
        rowcount, rowptr = self._rowcount, self._rowptr
        if diff_0 > 0:
            if self.has_rowcount():
                rowcount = torch.cat([rowcount, rowcount.new_zeros(diff_0)])
            if self.has_rowptr():
                rowptr = torch.cat([rowptr, rowptr.new_full((diff_0, ), nnz)])
        else:
            if self.has_rowcount():
                rowcount = rowcount[:-diff_0]
            if self.has_rowptr():
                rowptr = rowptr[:-diff_0]

        diff_1 = sizes[1] - old_sizes[1]
        colcount, colptr = self._colcount, self._colptr
        if diff_1 > 0:
            if self.has_colcount():
                colcount = torch.cat([colcount, colcount.new_zeros(diff_1)])
            if self.has_colptr():
                colptr = torch.cat([colptr, colptr.new_full((diff_1, ), nnz)])
        else:
            if self.has_colcount():
                colcount = colcount[:-diff_1]
            if self.has_colptr():
                colptr = colptr[:-diff_1]

        return self.__class__(
            self._index,
            self._value,
            sizes,
            rowcount=rowcount,
            rowptr=rowptr,
            colcount=colcount,
            colptr=colptr,
            csr2csc=self._csr2csc,
            csc2csr=self._csc2csr,
            is_sorted=True,
        )

    def has_rowcount(self):
        return self._rowcount is not None

    @cached_property
    def rowcount(self):
        rowptr = self.rowptr
        return rowptr[1:] - rowptr[:-1]

    def has_rowptr(self):
        return self._rowptr is not None

    @cached_property
    def rowptr(self):
        func = rowptr_cuda if self.index.is_cuda else rowptr_cpu
        return func.rowptr(self.row, self.sparse_size(0))

    def has_colcount(self):
        return self._colcount is not None

    @cached_property
    def colcount(self):
        if self.has_colptr():
            colptr = self.colptr
            return colptr[1:] - colptr[:-1]
        else:
            col, dim_size = self.col, self.sparse_size(1)
            return scatter_add(torch.ones_like(col), col, dim_size=dim_size)

    def has_colptr(self):
        return self._colptr is not None

    @cached_property
    def colptr(self):
        if self._csr2csc:
            func = rowptr_cuda if self.index.is_cuda else rowptr_cpu
            return func.rowptr(self.col[self.csr2csc], self.sparse_size(1))
        else:
            colcount = self.colcount
            colptr = colcount.new_zeros(colcount.size(0) + 1)
            torch.cumsum(colcount, dim=0, out=colptr[1:])
            return colptr

    def has_csr2csc(self):
        return self._csr2csc is not None

    @cached_property
    def csr2csc(self):
        idx = self._sparse_size[0] * self.col + self.row
        return idx.argsort()

    def has_csc2csr(self):
        return self._csc2csr is not None

    @cached_property
    def csc2csr(self):
        return self.csr2csc.argsort()

    def is_coalesced(self):
        idx = self.sparse_size(1) * self.row + self.col
        mask = idx > torch.cat([idx.new_full((1, ), -1), idx[:-1]], dim=0)
        return mask.all().item()

    def coalesce(self, reduce='add'):
        idx = self.sparse_size(1) * self.row + self.col
        mask = idx > torch.cat([idx.new_full((1, ), -1), idx[:-1]], dim=0)

        if mask.all():  # Skip if indices are already coalesced.
            return self

        index = self.index[:, mask]

        value = self.value
        if self.has_value():
            idx = mask.cumsum(0) - 1
            value = segment_csr(idx, value, reduce=reduce)
            value = value[0] if isinstance(value, tuple) else value

        return self.__class__(index, value, self.sparse_size(), is_sorted=True)

    def cached_keys(self):
        return [
            key for key in self.cache_keys
            if getattr(self, f'_{key}', None) is not None
        ]

    def fill_cache_(self, *args):
        for arg in args or self.cache_keys:
            getattr(self, arg)
        return self

    def clear_cache_(self, *args):
        for arg in args or self.cache_keys:
            setattr(self, f'_{arg}', None)
        return self

    def __copy__(self):
        return self.apply(lambda x: x)

    def clone(self):
        return self.apply(lambda x: x.clone())

    def __deepcopy__(self, memo):
        new_storage = self.clone()
        memo[id(self)] = new_storage
        return new_storage

    def apply_value_(self, func):
        self._value = optional(func, self._value)
        return self

    def apply_value(self, func):
        return self.__class__(
            self._index,
            optional(func, self._value),
            self._sparse_size,
            self._rowcount,
            self._rowptr,
            self._colcount,
            self._colptr,
            self._csr2csc,
            self._csc2csr,
            is_sorted=True,
        )

    def apply_(self, func):
        self._index = func(self._index)
        self._value = optional(func, self._value)
        for key in self.cached_keys():
            setattr(self, f'_{key}', func(getattr(self, f'_{key}')))
        return self

    def apply(self, func):
        return self.__class__(
            func(self._index),
            optional(func, self._value),
            self._sparse_size,
            optional(func, self._rowcount),
            optional(func, self._rowptr),
            optional(func, self._colcount),
            optional(func, self._colptr),
            optional(func, self._csr2csc),
            optional(func, self._csc2csr),
            is_sorted=True,
        )

    def map(self, func):
        data = [func(self.index)]
        if self.has_value():
            data += [func(self.value)]
        data += [func(getattr(self, f'_{key}')) for key in self.cached_keys()]
        return data
