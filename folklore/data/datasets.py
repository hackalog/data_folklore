import os
import pathlib
import sys

import joblib
from sklearn.datasets.base import Bunch
from sklearn.model_selection import train_test_split
from functools import partial

from ..paths import processed_data_path, data_path, raw_data_path, interim_data_path
from ..logging import logger
from .fetch import fetch_file, unpack, get_dataset_filename
from .utils import partial_call_signature, serialize_partial, deserialize_partial, process_dataset_default
from ..utils import load_json, save_json

__all__ = [
    'Dataset',
    'RawDataset',
    'add_raw_dataset',
    'available_datasets',
    'available_raw_datasets',
    'process_raw_datasets',
]

_MODULE = sys.modules[__name__]
_MODULE_DIR = pathlib.Path(os.path.dirname(os.path.abspath(__file__)))

def available_datasets(dataset_path=None, keys_only=True):
    """Get a list of available datasets.

    Parameters
    ----------
    dataset_path: path
        location of saved dataset files
    """
    if dataset_path is None:
        dataset_path = processed_data_path
    else:
        dataset_path = pathlib.Path(dataset_path)

    ds_dict = {}
    for dsfile in dataset_path.glob("*.metadata"):
        ds_stem = str(dsfile.stem)
        ds_meta = Dataset.load(ds_stem, data_path=dataset_path, metadata_only=True)
        ds_dict[ds_stem] = ds_meta

    if keys_only:
        return list(ds_dict.keys())
    return ds_dict


def process_raw_datasets(raw_datasets=None, action='process'):
    """Fetch, Unpack, and Process raw datasets.

    Parameters
    ----------
    raw_datasets: list or None
        List of raw dataset names to process.
        if None, loops over all available raw datasets.
    action: {'fetch', 'unpack', 'process'}
        Action to perform on raw datasets:
            'fetch': download raw files
            'unpack': unpack raw files
            'process': generate and cache Dataset objects
    """
    if raw_datasets is None:
        raw_datasets = available_raw_datasets()

    for dataset_name in raw_datasets:
        raw_ds = RawDataset.from_name(dataset_name)
        logger.info(f'Running {action} on {dataset_name}')
        if action == 'fetch':
            raw_ds.fetch()
        elif action == 'unpack':
            raw_ds.unpack()
        elif action == 'process':
            ds = raw_ds.process()
            logger.info(f'{dataset_name}: processed data has shape:{ds.data.shape}')

def add_raw_dataset(rawds):
    """Add a raw dataset to the list of available raw datasets"""

    rawds_list, rds_file_fq = available_raw_datasets(keys_only=False)
    rawds_list[rawds.name] = rawds.to_dict()
    save_json(rds_file_fq, rawds_list)

def available_raw_datasets(raw_dataset_file='raw_datasets.json',
                           raw_dataset_path=None, keys_only=True):
    """Returns the list of available datasets.

    Instructions for creating RawDatasets is stored in `raw_datasets.json` by default.

    keys_only: boolean
        if True, return a list of available datasets (default)
        if False, return complete dataset dictionary and filename

    Returns
    -------
    If `keys_only` is True:
        List of available dataset names
    else:
        Tuple (available_raw_dataset_dict, available_raw_dataset_dict_filename)
    """
    if raw_dataset_path is None:
        raw_dataset_path = _MODULE_DIR

    raw_dataset_file_fq = pathlib.Path(raw_dataset_path) / raw_dataset_file

    if not raw_dataset_file_fq.exists():
        raw_dataset_dict = {}
        logger.warning(f"No dataset file found: {raw_dataset_file}")
    else:
        raw_dataset_dict = load_json(raw_dataset_file_fq)

    if keys_only:
        return list(raw_dataset_dict.keys())

    return raw_dataset_dict, raw_dataset_file_fq


class Dataset(Bunch):
    def __init__(self, dataset_name=None, data=None, target=None, metadata=None, update_hashes=True,
                 **kwargs):
        """
        Object representing a dataset object.
        Notionally compatible with scikit-learn's Bunch object

        dataset_name: string (required)
            key to use for this dataset
        data:
            Data: (usually np.array or np.ndarray)
        target: np.array
            Either classification target or label to be used. for each of the points
            in `data`
        metadata: dict
            Data about the object. Key fields include `license_txt` and `descr`
        update_hashes:
            If True, update the data/target hashes in the Metadata.
        """
        super().__init__(**kwargs)

        if dataset_name is None:
            if metadata is not None and metadata.get("dataset_name", None) is not None:
                dataset_name = metadata['dataset_name']
            else:
                raise Exception('dataset_name is required')

        if metadata is not None:
            self['metadata'] = metadata
        else:
            self['metadata'] = {}
        self['metadata']['dataset_name'] = dataset_name
        self['data'] = data
        self['target'] = target
        if update_hashes:
            data_hashes = self.get_data_hashes()
            self['metadata'] = {**self['metadata'], **data_hashes}

    def __getattribute__(self, key):
        if key.isupper():
            try:
                return self['metadata'][key.lower()]
            except:
                raise AttributeError(key)
        else:
            return super().__getattribute__(key)

    def __setattr__(self, key, value):
        if key.isupper():
            self['metadata'][key.lower()] = value
        elif key == 'name':
            self['metadata']['dataset_name'] = value
        else:
            super().__setattr__(key, value)

    def __str__(self):
        s = f"<Dataset: {self.name}"
        if self.get('data', None) is not None:
            shape = getattr(self.data, 'shape', 'Unknown')
            s += f", data.shape={shape}"
        if self.get('target', None) is not None:
            shape = getattr(self.target, 'shape', 'Unknown')
            s += f", target.shape={shape}"
        meta = self.get('metadata', {})
        if meta:
            s += f", metadata={list(meta.keys())}"

        s += ">"
        return s

    @property
    def name(self):
        return self['metadata'].get('dataset_name', None)

    @name.setter
    def name(self, val):
        self['metadata']['dataset_name'] = val

    @property
    def has_target(self):
        return self['target'] is not None

    @classmethod
    def load(cls, file_base, data_path=None, metadata_only=False):
        """Load a dataset
        must be present in dataset.json"""

        if data_path is None:
            data_path = processed_data_path
        else:
            data_path = pathlib.Path(data_path)

        if metadata_only:
            metadata_fq = data_path / f'{file_base}.metadata'
            with open(metadata_fq, 'rb') as fd:
                meta = joblib.load(fd)
            return meta

        with open(data_path / f'{file_base}.dataset', 'rb') as fd:
            ds = joblib.load(fd)
        return ds

    @classmethod
    def from_raw(cls, dataset_name,
                 cache_path=None,
                 fetch_path=None,
                 force=False,
                 unpack_path=None,
                 **kwargs):
        '''Creates Dataset object from a named RawDataset.

        Dataset will be cached after creation. Subsequent calls with matching call
        signature will return this cached object.

        Parameters
        ----------
        dataset_name:
            Name of dataset to load. see `available_raw_datasets()` for the current list
            be returned (if available)
        cache_path: path
            Directory to search for Dataset cache files
        fetch_path: path
            Directory to download raw files into
        force: boolean
            If True, always regenerate the dataset. If false, a cached result can be returned
        unpack_path: path
            Directory to unpack raw files into
        **kwargs:
            Remaining keywords arguments are passed directly to RawDataset.process().
            See that docstring for details.

        Remaining keywords arguments are passed to the RawDataset's `process()` method
        '''
        dataset_list, _ = available_raw_datasets(keys_only=False)
        if dataset_name not in dataset_list:
            raise Exception(f'Unknown Dataset: {dataset_name}')
        raw_ds = RawDataset.from_dict(dataset_list[dataset_name])
        raw_ds.fetch(fetch_path=fetch_path, force=force)
        raw_ds.unpack(unpack_path=unpack_path, force=force)
        ds = raw_ds.process(cache_path=cache_path, force=force, **kwargs)

        return ds

    def get_data_hashes(self, exclude_list=None, hash_type='sha1'):
        """Compute a the hash of data items

        exclude_list: list or None
            List of attributes to skip.
            if None, skips ['metadata']

        hash_type: {'sha1', 'md5', 'sha256'}
            Algorithm to use for hashing
        """
        if exclude_list is None:
            exclude_list = ['metadata']

        ret = {'hash_type': hash_type}
        for key, value in self.items():
            if key in exclude_list:
                continue
            ret[f"{key}_hash"] = joblib.hash(value, hash_name=hash_type)
        return ret

    def dump(self, file_base=None, dump_path=None, hash_type='sha1',
             force=True, create_dirs=True, dump_metadata=True):
        """Dump a dataset.

        Note, this dumps a separate copy of the metadata structure,
        so that metadata can be looked up without loading the entire dataset,
        which could be large

        dump_metadata: boolean
            If True, also dump a standalone copy of the metadata.
            Useful for checking metadata without reading
            in the (potentially large) dataset itself
        file_base: string
            Filename stem. By default, just the dataset name
        hash_type: {'sha1', 'md5'}
            Hash function to use for hashing data/labels
        dump_path: path. (default: `processed_data_path`)
            Directory where data will be dumped.
        force: boolean
            If False, raise an exception if the file already exists
            If True, overwrite any existing files
        create_dirs: boolean
            If True, `dump_path` will be created (if necessary)

        """
        if dump_path is None:
            dump_path = processed_data_path
        dump_path = pathlib.Path(dump_path)

        if file_base is None:
            file_base = self.name

        metadata = self['metadata']

        metadata_filename = file_base + '.metadata'
        dataset_filename = file_base + '.dataset'
        metadata_fq = dump_path / metadata_filename

        data_hashes = self.get_data_hashes(hash_type=hash_type)
        self['metadata'] = {**self['metadata'], **data_hashes}

        # check for a cached version
        if metadata_fq.exists() and force is not True:
            logger.warning(f"Existing metatdata file found: {metadata_fq}")
            cached_metadata = joblib.load(metadata_fq)
            # are we a subset of the cached metadata? (Py3+ only)
            if metadata.items() <= cached_metadata.items():
                raise Exception(f'Dataset with matching metadata exists already. '
                                'Use `force=True` to overwrite, or change one of '
                                '`dataset.metadata` or `file_base`')
            else:
                raise Exception(f'Metadata file {metadata_filename} exists '
                                'but metadata has changed. '
                                'Use `force=True` to overwrite, or change '
                                '`file_base`')

        if create_dirs:
            os.makedirs(metadata_fq.parent, exist_ok=True)

        if dump_metadata:
            with open(metadata_fq, 'wb') as fo:
                joblib.dump(metadata, fo)
            logger.debug(f'Wrote Dataset Metadata: {metadata_filename}')

        dataset_fq = dump_path / dataset_filename
        with open(dataset_fq, 'wb') as fo:
            joblib.dump(self, fo)
        logger.debug(f'Wrote Dataset: {dataset_filename}')


class RawDataset(object):
    """Representation of a raw dataset"""

    def __init__(self,
                 name='raw_dataset',
                 load_function=None,
                 dataset_dir=None,
                 file_list=None):
        """Create a RawDataset
        Parameters
        ----------
        name: str
            name of dataset
        load_function: func (or partial)
            Function that will be called to process raw data into usable Dataset
        dataset_dir: path
            default location for raw files
        file_list: list
            list of file_dicts associated with this RawDataset.
            Valid keys for each file_dict include:
                url: (optional)
                    URL of resource to be fetched
                hash_type: {'sha1', 'md5', 'sha256'}
                    Type of hash function used to verify file integrity
                hash_value: string
                    Value of hash used to verify file integrity
                file_name: string (optional)
                    filename to use when saving file locally.
                name: string or {'DESCR', 'LICENSE'} (optional)
                    description of the file. of DESCR or LICENSE, will be used as metadata
        """
        if file_list is None:
            file_list = []
        if dataset_dir is None:
            dataset_dir = raw_data_path
        if load_function is None:
            load_function = process_dataset_default
        self.name = name
        self.file_list = file_list
        self.load_function = load_function
        self.dataset_dir = dataset_dir

        # sklearn-style attributes. Usually these would be set in fit()
        self.fetched_ = False
        self.fetched_files_ = []
        self.unpacked_ = False
        self.unpack_path_ = None

    def add_metadata(self, filename=None, contents=None, metadata_path=None, kind='DESCR'):
        """Add metadata to a raw dataset

        filename: create metadata entry from contents of this file
        contents: create metadata entry from this string
        metadata_path: (default `raw_data_path`)
            Where to store metadata
        kind: {'DESCR', 'LICENSE'}
        """
        if metadata_path is None:
            metadata_path = raw_data_path
        else:
            metadata_path = pathlib.Path(metadata_path)
        filename_map = {
            'DESCR': f'{self.name}.readme',
            'LICENSE': f'{self.name}.license',
        }
        if kind not in filename_map:
            raise Exception(f'Unknown kind: {kind}. Must be one of {filename_map.keys()}')

        if filename is not None:
            filelist_entry = {
                'file_name': filename,
                'name': kind
                }
        elif contents is not None:
            filelist_entry = {
                'contents': contents,
                'file_name': filename_map[kind],
                'name': kind,
            }
        else:
            raise Exception(f'One of `filename` or `contents` is required')

        self.file_list.append(filelist_entry)
        self.fetched_ = False

    def add_file(self, hash_type='sha1', hash_value=None,
                 name=None, *, file_name):
        """
        Add a file to the file list.

        This file must exist on disk, as there is no method specified for fetching it.
        This is useful when the raw dataset requires an offline procedure for downloading.

        hash_type: {'sha1', 'md5', 'sha256'}
        hash_value: string or None
            if None, hash will be computed from specified file
        file_name: string
            Name of downloaded file.
        name: str
            text description of this file.
        """
        fq_file = pathlib.Path(self.dataset_dir) / file_name
        if not fq_file.exists():
            logger.warning(f"{file_name} not found on disk")
        fetch_dict = {'hash_type':hash_type,
                      'hash_value':hash_value,
                      'name': name,
                      'file_name':file_name}
        self.file_list.append(fetch_dict)
        self.fetched_ = False

    def add_url(self, url=None, hash_type='sha1', hash_value=None,
                name=None, file_name=None):
        """
        Add a URL to the file list

        hash_type: {'sha1', 'md5', 'sha256'}
        hash_value: string or None
            if None, hash will be computed from downloaded file
        file_name: string or None
            Name of downloaded file. If None, will be the last component of the URL
        url: string
            URL to fetch
        name: str
            text description of this file.
        """

        fetch_dict = {'url': url,
                      'hash_type':hash_type,
                      'hash_value':hash_value,
                      'name': name,
                      'file_name':file_name}
        self.file_list.append(fetch_dict)
        self.fetched_ = False

    def fetch(self, fetch_path=None, force=False):
        """Fetch to raw_data_dir and check hashes
        """
        if self.fetched_ and force is False:
            logger.debug(f'Raw Dataset {self.name} is already fetched. Skipping')
            return

        if fetch_path is None:
            fetch_path = self.dataset_dir
        else:
            fetch_path = pathlib.Path(fetch_path)

        self.fetched_ = False
        self.fetched_files_ = []
        for item in self.file_list:
            status, result, hash_value = fetch_file(**item)
            if status:
                item['hash_value'] = hash_value
                self.fetched_files_.append(result)
            else:
                if item.get('url', False):
                    logger.error(f"fetch of {item['url']} returned: {result}")
                    break
        else:
            self.fetched_ = True

        return self.fetched_


    def unpack(self, unpack_path=None, force=False):
        """Unpack fetched files to interim dir"""
        if not self.fetched_:
            logger.debug("unpack() called before fetch()")
            self.fetch()

        if self.unpacked_ and force is False:
            logger.debug(f'Raw Dataset {self.name} is already unpacked. Skipping')
        else:
            if unpack_path is None:
                unpack_path = interim_data_path / self.name
            else:
                unpack_path = pathlib.Path(unpack_path)
            for filename in self.fetched_files_:
                unpack(filename, dst_dir=unpack_path)
            self.unpacked_ = True
            self.unpack_path_ = unpack_path

        return self.unpack_path_

    def process(self,
                cache_path=None,
                force=False,
                return_X_y=False,
                use_docstring=False,
                **kwargs):
        """Turns the raw dataset into a fully-processed Dataset object.

        This generated Dataset object is cached using joblib, so subsequent
        calls to process with the same file_list and kwargs should be fast.

        Parameters
        ----------
        cache_path: path
            Location of joblib cache.
        force: boolean
            If False, use a cached object (if available).
            If True, regenerate object from scratch.
        return_X_y: boolean
            if True, returns (data, target) instead of a `Dataset` object.
        use_docstring: boolean
            If True, the docstring of `self.load_function` is used as the Dataset DESCR text.
        """
        if not self.unpacked_:
            logger.debug("process() called before unpack()")
            self.unpack()

        if cache_path is None:
            cache_path = interim_data_path
        else:
            cache_path = pathlib.Path(cache_path)

        # If any of these things change, recreate and cache a new Dataset

        meta_hash = self.to_hash(**kwargs)

        dset = None
        dset_opts = {}
        if force is False:
            try:
                dset = Dataset.load(meta_hash, data_path=cache_path)
                logger.debug(f"Found cached Dataset for {self.name}: {meta_hash}")
            except FileNotFoundError:
                logger.debug(f"No cached Dataset found. Re-creating {self.name}")

        if dset is None:
            metadata = self.default_metadata(use_docstring=use_docstring)
            supplied_metadata = kwargs.pop('metadata', {})
            kwargs['metadata'] = {**metadata, **supplied_metadata}
            dset_opts = self.load_function(**kwargs)
            dset = Dataset(**dset_opts)
            dset.dump(dump_path=cache_path, file_base=meta_hash)

        if return_X_y:
            return dset.data, dset.target

        return dset


    def default_metadata(self, use_docstring=False):
        """Returns default metadata derived from this RawDataset

        This sets the dataset_name, and fills in `license` and `descr`
        fields if they are present, either on disk, or in the file list

        Parameters
        ----------
        use_docstring: boolean
            If True, the docstring of `self.load_function` is used as the Dataset DESCR text.

        Returns
        -------
        Dict of metadata key/value pairs
        """

        metadata = {}
        optmap = {
            'DESCR': 'descr',
            'LICENSE': 'license',
        }
        filemap = {
            'license': f'{self.name}.license',
            'descr': f'{self.name}.readme'
        }

        for fetch_dict in self.file_list:
            name = fetch_dict.get('name', None)
            # if metadata is present in the URL list, use it
            if name in optmap:
                txtfile = get_dataset_filename(fetch_dict)
                with open(raw_data_path / txtfile, 'r') as fr:
                    metadata[optmap[name]] = fr.read()
        if use_docstring:
            func = partial(self.load_function)
            fqfunc, invocation =  partial_call_signature(func)
            metadata['descr'] =  f'Data processed by: {fqfunc}\n\n>>> ' + \
              f'{invocation}\n\n>>> help({func.func.__name__})\n\n' + \
              f'{func.func.__doc__}'

        metadata['dataset_name'] = self.name
        return metadata

    def to_hash(self, ignore=None, hash_type='sha1', **kwargs):
        """Compute a hash for this object.

        converts this object to a dict, and hashes the result,
        adding or removing keys as specified.

        hash_type: {'md5', 'sha1', 'sha256'}
            Hash algorithm to use
        ignore: list
            list of keys to ignore
        kwargs:
            key/value pairs to add before hashing
        """
        if ignore is None:
            ignore = ['dataset_dir']
        my_dict = {**self.to_dict(), **kwargs}
        for key in ignore:
            my_dict.pop(key, None)

        return joblib.hash(my_dict, hash_name=hash_type)

    def __hash__(self):
        return hash(self.to_hash())

    def to_dict(self):
        """Convert a RawDataset to a serializable dictionary"""
        load_function_dict = serialize_partial(self.load_function)
        obj_dict = {
            'url_list': self.file_list,
            **load_function_dict,
            'name': self.name,
            'dataset_dir': str(self.dataset_dir)
        }
        return obj_dict

    @classmethod
    def from_name(cls, raw_dataset_name,
                  raw_dataset_file='raw_datasets.json',
                  raw_dataset_path=None):
        """Create a RawDataset from a dictionary key name.

        The `raw_dataset_file` is a json file mapping raw_dataset_name
        to its dictionary representation.

        Parameters
        ----------
        raw_dataset_name: str
            Name of raw dataset. Used as the key in the on-disk key_file
        key_file_path:
            Location of key_file (json dict containing raw dataset defintion)
            if None, use source code module: src/data/{key_file_name}
        key_file_name:
            Name of json file containing key/dict map

        """
        raw_datasets, _ = available_raw_datasets(raw_dataset_file=raw_dataset_file,
                                                 raw_dataset_path=raw_dataset_path,
                                                 keys_only=False)
        return cls.from_dict(raw_datasets[raw_dataset_name])

    @classmethod
    def from_dict(cls, obj_dict):
        """Create a RawDataset from a dictionary.

        name: str
            dataset name
        dataset_dir: path
            pathname to load and save dataset
        obj_dict: dict
            Should contain url_list, and load_function_{name|module|args|kwargs} keys,
            name, and dataset_dir
        """
        file_list = obj_dict.get('url_list', [])
        load_function = deserialize_partial(obj_dict)
        name = obj_dict['name']
        dataset_dir = obj_dict.get('dataset_dir', None)
        return cls(name=name,
                   load_function=load_function,
                   dataset_dir=dataset_dir,
                   file_list=file_list)
