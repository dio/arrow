# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import contextlib
import os
import pathlib
import pickle

import numpy as np
import pytest

import pyarrow as pa
import pyarrow.csv
import pyarrow.fs as fs

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    import pyarrow.dataset as ds
except ImportError:
    ds = None

# Marks all of the tests in this module
# Ignore these with pytest ... -m 'not dataset'
pytestmark = pytest.mark.dataset


@contextlib.contextmanager
def change_cwd(path):
    curdir = os.getcwd()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(curdir)


def _generate_data(n):
    import datetime
    import itertools

    day = datetime.datetime(2000, 1, 1)
    interval = datetime.timedelta(days=5)
    colors = itertools.cycle(['green', 'blue', 'yellow', 'red', 'orange'])

    data = []
    for i in range(n):
        data.append((day, i, float(i), next(colors)))
        day += interval

    return pd.DataFrame(data, columns=['date', 'index', 'value', 'color'])


def _table_from_pandas(df):
    schema = pa.schema([
        pa.field('date', pa.date32()),
        pa.field('index', pa.int64()),
        pa.field('value', pa.float64()),
        pa.field('color', pa.string()),
    ])
    table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    return table.replace_schema_metadata()


def _filesystem_uri(path):
    # URIs on Windows must follow 'file:///C:...' or 'file:/C:...' patterns.
    if os.name == 'nt':
        uri = 'file:///{}'.format(path)
    else:
        uri = 'file://{}'.format(path)
    return uri


@pytest.fixture
@pytest.mark.parquet
def mockfs():
    import pyarrow.parquet as pq

    mockfs = fs._MockFileSystem()

    directories = [
        'subdir/1/xxx',
        'subdir/2/yyy',
    ]

    for i, directory in enumerate(directories):
        path = '{}/file{}.parquet'.format(directory, i)
        mockfs.create_dir(directory)
        with mockfs.open_output_stream(path) as out:
            data = [
                list(range(5)),
                list(map(float, range(5))),
                list(map(str, range(5))),
                [i] * 5
            ]
            schema = pa.schema([
                pa.field('i64', pa.int64()),
                pa.field('f64', pa.float64()),
                pa.field('str', pa.string()),
                pa.field('const', pa.int64()),
            ])
            batch = pa.record_batch(data, schema=schema)
            table = pa.Table.from_batches([batch])

            pq.write_table(table, out)

    return mockfs


@pytest.fixture
def open_logging_fs(monkeypatch):
    from pyarrow.fs import PyFileSystem, LocalFileSystem, _normalize_path
    from .test_fs import ProxyHandler

    localfs = LocalFileSystem()

    def normalized(paths):
        return {_normalize_path(localfs, str(p)) for p in paths}

    opened = set()

    def open_input_file(self, path):
        path = _normalize_path(localfs, str(path))
        opened.add(path)
        return self._fs.open_input_file(path)

    # patch proxyhandler to log calls to open_input_file
    monkeypatch.setattr(ProxyHandler, "open_input_file", open_input_file)
    fs = PyFileSystem(ProxyHandler(localfs))

    @contextlib.contextmanager
    def assert_opens(expected_opened):
        opened.clear()
        try:
            yield
        finally:
            assert normalized(opened) == normalized(expected_opened)

    return fs, assert_opens


@pytest.fixture(scope='module')
def multisourcefs(request):
    request.config.pyarrow.requires('pandas')
    request.config.pyarrow.requires('parquet')
    import pyarrow.parquet as pq

    df = _generate_data(1000)
    mockfs = fs._MockFileSystem()

    # simply split the dataframe into three chunks to construct a data source
    # from each chunk into its own directory
    df_a, df_b, df_c, df_d = np.array_split(df, 4)

    # create a directory containing a flat sequence of parquet files without
    # any partitioning involved
    mockfs.create_dir('plain')
    for i, chunk in enumerate(np.array_split(df_a, 10)):
        path = 'plain/chunk-{}.parquet'.format(i)
        with mockfs.open_output_stream(path) as out:
            pq.write_table(_table_from_pandas(chunk), out)

    # create one with schema partitioning by week and color
    mockfs.create_dir('schema')
    for part, chunk in df_b.groupby([df_b.date.dt.week, df_b.color]):
        folder = 'schema/{}/{}'.format(*part)
        path = '{}/chunk.parquet'.format(folder)
        mockfs.create_dir(folder)
        with mockfs.open_output_stream(path) as out:
            pq.write_table(_table_from_pandas(chunk), out)

    # create one with hive partitioning by year and month
    mockfs.create_dir('hive')
    for part, chunk in df_c.groupby([df_c.date.dt.year, df_c.date.dt.month]):
        folder = 'hive/year={}/month={}'.format(*part)
        path = '{}/chunk.parquet'.format(folder)
        mockfs.create_dir(folder)
        with mockfs.open_output_stream(path) as out:
            pq.write_table(_table_from_pandas(chunk), out)

    # create one with hive partitioning by color
    mockfs.create_dir('hive_color')
    for part, chunk in df_d.groupby(["color"]):
        folder = 'hive_color/color={}'.format(*part)
        path = '{}/chunk.parquet'.format(folder)
        mockfs.create_dir(folder)
        with mockfs.open_output_stream(path) as out:
            pq.write_table(_table_from_pandas(chunk), out)

    return mockfs


@pytest.fixture
def dataset(mockfs):
    format = ds.ParquetFileFormat()
    selector = fs.FileSelector('subdir', recursive=True)
    options = ds.FileSystemFactoryOptions('subdir')
    options.partitioning = ds.DirectoryPartitioning(
        pa.schema([
            pa.field('group', pa.int32()),
            pa.field('key', pa.string())
        ])
    )
    factory = ds.FileSystemDatasetFactory(mockfs, selector, format, options)
    return factory.finish()


def test_filesystem_dataset(mockfs):
    schema = pa.schema([
        pa.field('const', pa.int64())
    ])
    file_format = ds.ParquetFileFormat()
    paths = ['subdir/1/xxx/file0.parquet', 'subdir/2/yyy/file1.parquet']
    partitions = [ds.field('part') == x for x in range(1, 3)]
    fragments = [file_format.make_fragment(path, mockfs, part)
                 for path, part in zip(paths, partitions)]
    root_partition = ds.field('level') == ds.scalar(1337)

    dataset_from_fragments = ds.FileSystemDataset(
        fragments, schema=schema, format=file_format,
        root_partition=root_partition,
    )
    dataset_from_paths = ds.FileSystemDataset.from_paths(
        paths, schema=schema, format=file_format, filesystem=mockfs,
        partitions=partitions, root_partition=root_partition,
    )

    for dataset in [dataset_from_fragments, dataset_from_paths]:
        assert isinstance(dataset, ds.FileSystemDataset)
        assert isinstance(dataset.format, ds.ParquetFileFormat)
        assert dataset.partition_expression.equals(root_partition)
        assert set(dataset.files) == set(paths)

        fragments = list(dataset.get_fragments())
        for fragment, partition, path in zip(fragments, partitions, paths):
            assert fragment.partition_expression.equals(partition)
            assert fragment.path == path
            assert isinstance(fragment.format, ds.ParquetFileFormat)
            assert isinstance(fragment, ds.ParquetFileFragment)
            assert fragment.row_groups is None

            row_group_fragments = list(fragment.split_by_row_group())
            assert len(row_group_fragments) == 1
            assert isinstance(row_group_fragments[0], ds.ParquetFileFragment)
            assert row_group_fragments[0].path == path
            assert row_group_fragments[0].row_groups == [ds.RowGroupInfo(0)]

        fragments = list(dataset.get_fragments(filter=ds.field("const") == 0))
        assert len(fragments) == 2

    # the root_partition keyword has a default
    dataset = ds.FileSystemDataset(
        fragments, schema=schema, format=file_format
    )
    assert dataset.partition_expression.equals(ds.scalar(True))

    # from_paths partitions have defaults
    dataset = ds.FileSystemDataset.from_paths(
        paths, schema=schema, format=file_format, filesystem=mockfs
    )
    assert dataset.partition_expression.equals(ds.scalar(True))
    for fragment in dataset.get_fragments():
        assert fragment.partition_expression.equals(ds.scalar(True))

    # validation of required arguments
    with pytest.raises(TypeError, match="incorrect type"):
        ds.FileSystemDataset(fragments, file_format, schema)
    # validation of root_partition
    with pytest.raises(TypeError, match="incorrect type"):
        ds.FileSystemDataset(fragments, schema=schema, format=file_format,
                             root_partition=1)
    # missing required argument in from_paths
    with pytest.raises(TypeError, match="incorrect type"):
        ds.FileSystemDataset.from_paths(fragments, format=file_format)


def test_filesystem_dataset_no_filesystem_interaction():
    # ARROW-8283
    schema = pa.schema([
        pa.field('f1', pa.int64())
    ])
    file_format = ds.IpcFileFormat()
    paths = ['nonexistingfile.arrow']

    # creating the dataset itself doesn't raise
    dataset = ds.FileSystemDataset.from_paths(
        paths, schema=schema, format=file_format,
        filesystem=fs.LocalFileSystem(),
    )

    # getting fragments also doesn't raise
    dataset.get_fragments()

    # scanning does raise
    with pytest.raises(FileNotFoundError):
        dataset.to_table()


def test_dataset(dataset):
    assert isinstance(dataset, ds.Dataset)
    assert isinstance(dataset.schema, pa.Schema)

    # TODO(kszucs): test non-boolean Exprs for filter do raise
    expected_i64 = pa.array([0, 1, 2, 3, 4], type=pa.int64())
    expected_f64 = pa.array([0, 1, 2, 3, 4], type=pa.float64())
    for task in dataset.scan():
        assert isinstance(task, ds.ScanTask)
        for batch in task.execute():
            assert batch.column(0).equals(expected_i64)
            assert batch.column(1).equals(expected_f64)

    batches = dataset.to_batches()
    assert all(isinstance(batch, pa.RecordBatch) for batch in batches)

    table = dataset.to_table()
    assert isinstance(table, pa.Table)
    assert len(table) == 10

    condition = ds.field('i64') == 1
    result = dataset.to_table(use_threads=True, filter=condition).to_pydict()

    # don't rely on the scanning order
    assert result['i64'] == [1, 1]
    assert result['f64'] == [1., 1.]
    assert sorted(result['group']) == [1, 2]
    assert sorted(result['key']) == ['xxx', 'yyy']


def test_scanner(dataset):
    scanner = ds.Scanner.from_dataset(dataset,
                                      memory_pool=pa.default_memory_pool())
    assert isinstance(scanner, ds.Scanner)
    assert len(list(scanner.scan())) == 2

    with pytest.raises(pa.ArrowInvalid):
        ds.Scanner.from_dataset(dataset, columns=['unknown'])

    scanner = ds.Scanner.from_dataset(dataset, columns=['i64'],
                                      memory_pool=pa.default_memory_pool())

    assert isinstance(scanner, ds.Scanner)
    assert len(list(scanner.scan())) == 2
    for task in scanner.scan():
        for batch in task.execute():
            assert batch.num_columns == 1


def test_abstract_classes():
    classes = [
        ds.FileFormat,
        ds.Scanner,
        ds.Partitioning,
    ]
    for klass in classes:
        with pytest.raises(TypeError):
            klass()


def test_partitioning():
    schema = pa.schema([
        pa.field('i64', pa.int64()),
        pa.field('f64', pa.float64())
    ])
    for klass in [ds.DirectoryPartitioning, ds.HivePartitioning]:
        partitioning = klass(schema)
        assert isinstance(partitioning, ds.Partitioning)

    partitioning = ds.DirectoryPartitioning(
        pa.schema([
            pa.field('group', pa.int64()),
            pa.field('key', pa.float64())
        ])
    )
    expr = partitioning.parse('/3/3.14')
    assert isinstance(expr, ds.Expression)

    expected = (ds.field('group') == 3) & (ds.field('key') == 3.14)
    assert expr.equals(expected)

    with pytest.raises(pa.ArrowInvalid):
        partitioning.parse('/prefix/3/aaa')

    partitioning = ds.HivePartitioning(
        pa.schema([
            pa.field('alpha', pa.int64()),
            pa.field('beta', pa.int64())
        ])
    )
    expr = partitioning.parse('/alpha=0/beta=3')
    expected = (
        (ds.field('alpha') == ds.scalar(0)) &
        (ds.field('beta') == ds.scalar(3))
    )
    assert expr.equals(expected)

    for shouldfail in ['/alpha=one/beta=2', '/alpha=one', '/beta=two']:
        with pytest.raises(pa.ArrowInvalid):
            partitioning.parse(shouldfail)


def test_expression_serialization():
    a = ds.scalar(1)
    b = ds.scalar(1.1)
    c = ds.scalar(True)
    d = ds.scalar("string")
    e = ds.scalar(None)

    condition = ds.field('i64') > 5
    schema = pa.schema([
        pa.field('i64', pa.int64()),
        pa.field('f64', pa.float64())
    ])
    assert condition.validate(schema) == pa.bool_()

    assert condition.assume(ds.field('i64') == 5).equals(
        ds.scalar(False))

    assert condition.assume(ds.field('i64') == 7).equals(
        ds.scalar(True))

    all_exprs = [a, b, c, d, e, a == b, a > b, a & b, a | b, ~c,
                 d.is_valid(), a.cast(pa.int32(), safe=False),
                 a.cast(pa.int32(), safe=False), a.isin([1, 2, 3]),
                 ds.field('i64') > 5, ds.field('i64') == 5,
                 ds.field('i64') == 7]
    for expr in all_exprs:
        assert isinstance(expr, ds.Expression)
        restored = pickle.loads(pickle.dumps(expr))
        assert expr.equals(restored)


def test_expression_construction():
    zero = ds.scalar(0)
    one = ds.scalar(1)
    true = ds.scalar(True)
    false = ds.scalar(False)
    string = ds.scalar("string")
    field = ds.field("field")

    zero | one == string
    ~true == false
    for typ in ("bool", pa.bool_()):
        field.cast(typ) == true

    field.isin([1, 2])

    with pytest.raises(TypeError):
        field.isin(1)

    # operations with non-scalar values
    with pytest.raises(TypeError):
        field == [1]

    with pytest.raises(TypeError):
        field != {1}

    with pytest.raises(TypeError):
        field & [1]

    with pytest.raises(TypeError):
        field | [1]


def test_parquet_read_options():
    opts1 = ds.ParquetReadOptions()
    opts2 = ds.ParquetReadOptions(buffer_size=4096,
                                  dictionary_columns=['a', 'b'])
    opts3 = ds.ParquetReadOptions(buffer_size=2**13, use_buffered_stream=True,
                                  dictionary_columns={'a', 'b'})

    assert opts1.use_buffered_stream is False
    assert opts1.buffer_size == 2**13
    assert opts1.dictionary_columns == set()

    assert opts2.use_buffered_stream is False
    assert opts2.buffer_size == 2**12
    assert opts2.dictionary_columns == {'a', 'b'}

    assert opts3.use_buffered_stream is True
    assert opts3.buffer_size == 2**13
    assert opts3.dictionary_columns == {'a', 'b'}

    assert opts1 == opts1
    assert opts1 != opts2
    assert opts2 != opts3


def test_file_format_pickling():
    formats = [
        ds.IpcFileFormat(),
        ds.CsvFileFormat(),
        ds.CsvFileFormat(pa.csv.ParseOptions(delimiter='\t',
                                             ignore_empty_lines=True)),
        ds.ParquetFileFormat(),
        ds.ParquetFileFormat(
            read_options=ds.ParquetReadOptions(use_buffered_stream=True)
        ),
        ds.ParquetFileFormat(
            read_options={
                'use_buffered_stream': True,
                'buffer_size': 4096,
            }
        )
    ]
    for file_format in formats:
        assert pickle.loads(pickle.dumps(file_format)) == file_format


@pytest.mark.parametrize('paths_or_selector', [
    fs.FileSelector('subdir', recursive=True),
    [
        'subdir/1/xxx/file0.parquet',
        'subdir/2/yyy/file1.parquet',
    ]
])
def test_filesystem_factory(mockfs, paths_or_selector):
    format = ds.ParquetFileFormat(
        read_options=ds.ParquetReadOptions(dictionary_columns={"str"})
    )

    options = ds.FileSystemFactoryOptions('subdir')
    options.partitioning = ds.DirectoryPartitioning(
        pa.schema([
            pa.field('group', pa.int32()),
            pa.field('key', pa.string())
        ])
    )
    assert options.partition_base_dir == 'subdir'
    assert options.selector_ignore_prefixes == ['.', '_']
    assert options.exclude_invalid_files is False

    factory = ds.FileSystemDatasetFactory(
        mockfs, paths_or_selector, format, options
    )
    inspected_schema = factory.inspect()

    assert factory.inspect().equals(pa.schema([
        pa.field('i64', pa.int64()),
        pa.field('f64', pa.float64()),
        pa.field('str', pa.dictionary(pa.int32(), pa.string())),
        pa.field('const', pa.int64()),
        pa.field('group', pa.int32()),
        pa.field('key', pa.string()),
    ]), check_metadata=False)

    assert isinstance(factory.inspect_schemas(), list)
    assert isinstance(factory.finish(inspected_schema),
                      ds.FileSystemDataset)
    assert factory.root_partition.equals(ds.scalar(True))

    dataset = factory.finish()
    assert isinstance(dataset, ds.FileSystemDataset)
    assert len(list(dataset.scan())) == 2

    scanner = ds.Scanner.from_dataset(dataset)
    expected_i64 = pa.array([0, 1, 2, 3, 4], type=pa.int64())
    expected_f64 = pa.array([0, 1, 2, 3, 4], type=pa.float64())
    expected_str = pa.DictionaryArray.from_arrays(
        pa.array([0, 1, 2, 3, 4], type=pa.int32()),
        pa.array("0 1 2 3 4".split(), type=pa.string())
    )
    for task, group, key in zip(scanner.scan(), [1, 2], ['xxx', 'yyy']):
        expected_group = pa.array([group] * 5, type=pa.int32())
        expected_key = pa.array([key] * 5, type=pa.string())
        expected_const = pa.array([group - 1] * 5, type=pa.int64())
        for batch in task.execute():
            assert batch.num_columns == 6
            assert batch[0].equals(expected_i64)
            assert batch[1].equals(expected_f64)
            assert batch[2].equals(expected_str)
            assert batch[3].equals(expected_const)
            assert batch[4].equals(expected_group)
            assert batch[5].equals(expected_key)

    table = dataset.to_table()
    assert isinstance(table, pa.Table)
    assert len(table) == 10
    assert table.num_columns == 6


def test_make_fragment(multisourcefs):
    parquet_format = ds.ParquetFileFormat()
    dataset = ds.dataset('/plain', filesystem=multisourcefs,
                         format=parquet_format)

    for path in dataset.files:
        fragment = parquet_format.make_fragment(path, multisourcefs)
        row_group_fragment = parquet_format.make_fragment(path, multisourcefs,
                                                          row_groups=[0])
        for f in [fragment, row_group_fragment]:
            assert isinstance(f, ds.ParquetFileFragment)
            assert f.path == path
            assert isinstance(f.filesystem, type(multisourcefs))
        assert fragment.row_groups is None
        assert row_group_fragment.row_groups == [ds.RowGroupInfo(0)]


def _create_dataset_for_fragments(tempdir, chunk_size=None):
    import pyarrow.parquet as pq

    table = pa.table(
        [range(8), [1] * 8, ['a'] * 4 + ['b'] * 4],
        names=['f1', 'f2', 'part']
    )

    path = str(tempdir / "test_parquet_dataset")

    # write_to_dataset currently requires pandas
    pq.write_to_dataset(table, path,
                        partition_cols=["part"], chunk_size=chunk_size)

    return table, ds.dataset(path, format="parquet", partitioning="hive")


@pytest.mark.pandas
@pytest.mark.parquet
def test_fragments(tempdir):
    table, dataset = _create_dataset_for_fragments(tempdir)

    # list fragments
    fragments = list(dataset.get_fragments())
    assert len(fragments) == 2
    f = fragments[0]

    physical_names = ['f1', 'f2']
    # file's schema does not include partition column
    assert f.physical_schema.names == physical_names
    assert f.format.inspect(f.path, f.filesystem) == f.physical_schema
    assert f.partition_expression.equals(ds.field('part') == 'a')

    # By default, the partition column is not part of the schema.
    result = f.to_table()
    assert result.column_names == physical_names
    assert result.equals(table.remove_column(2).slice(0, 4))

    # scanning fragment includes partition columns when given the proper
    # schema.
    result = f.to_table(schema=dataset.schema)
    assert result.column_names == ['f1', 'f2', 'part']
    assert result.equals(table.slice(0, 4))
    assert f.physical_schema == result.schema.remove(2)

    # scanning fragments follow filter predicate
    result = f.to_table(schema=dataset.schema, filter=ds.field('f1') < 2)
    assert result.column_names == ['f1', 'f2', 'part']


@pytest.mark.pandas
@pytest.mark.parquet
def test_fragments_implicit_cast(tempdir):
    # ARROW-8693
    import pyarrow.parquet as pq

    table = pa.table([range(8), [1] * 4 + [2] * 4], names=['col', 'part'])
    path = str(tempdir / "test_parquet_dataset")
    pq.write_to_dataset(table, path, partition_cols=["part"])

    part = ds.partitioning(pa.schema([('part', 'int8')]), flavor="hive")
    dataset = ds.dataset(path, format="parquet", partitioning=part)
    fragments = dataset.get_fragments(filter=ds.field("part") >= 2)
    assert len(list(fragments)) == 1


@pytest.mark.pandas
@pytest.mark.parquet
def test_fragments_reconstruct(tempdir):
    table, dataset = _create_dataset_for_fragments(tempdir)

    def assert_yields_projected(fragment, row_slice,
                                columns=None, filter=None):
        actual = fragment.to_table(
            schema=table.schema, columns=columns, filter=filter)
        column_names = columns if columns else table.column_names
        assert actual.column_names == column_names

        expected = table.slice(*row_slice).to_pandas()[[*column_names]]
        assert actual.equals(pa.Table.from_pandas(expected))

    fragment = list(dataset.get_fragments())[0]
    parquet_format = fragment.format

    # manually re-construct a fragment, with explicit schema
    new_fragment = parquet_format.make_fragment(
        fragment.path, fragment.filesystem,
        partition_expression=fragment.partition_expression)
    assert new_fragment.to_table().equals(fragment.to_table())
    assert_yields_projected(new_fragment, (0, 4))

    # filter / column projection, inspected schema
    new_fragment = parquet_format.make_fragment(
        fragment.path, fragment.filesystem,
        partition_expression=fragment.partition_expression)
    assert_yields_projected(new_fragment, (0, 2), filter=ds.field('f1') < 2)

    # filter requiring cast / column projection, inspected schema
    new_fragment = parquet_format.make_fragment(
        fragment.path, fragment.filesystem,
        partition_expression=fragment.partition_expression)
    assert_yields_projected(new_fragment, (0, 2),
                            columns=['f1'], filter=ds.field('f1') < 2.0)

    # filter on the partition column
    new_fragment = parquet_format.make_fragment(
        fragment.path, fragment.filesystem,
        partition_expression=fragment.partition_expression)
    assert_yields_projected(new_fragment, (0, 4),
                            filter=ds.field('part') == 'a')

    # Fragments don't contain the partition's columns if not provided to the
    # `to_table(schema=...)` method.
    with pytest.raises(ValueError, match="Field named 'part' not found"):
        new_fragment = parquet_format.make_fragment(
            fragment.path, fragment.filesystem,
            partition_expression=fragment.partition_expression)
        new_fragment.to_table(filter=ds.field('part') == 'a')


@pytest.mark.pandas
@pytest.mark.parquet
def test_fragments_parquet_row_groups(tempdir):
    table, dataset = _create_dataset_for_fragments(tempdir, chunk_size=2)

    fragment = list(dataset.get_fragments())[0]

    # list and scan row group fragments
    row_group_fragments = list(fragment.split_by_row_group())
    assert len(row_group_fragments) == 2
    result = row_group_fragments[0].to_table(schema=dataset.schema)
    assert result.column_names == ['f1', 'f2', 'part']
    assert len(result) == 2
    assert result.equals(table.slice(0, 2))

    assert row_group_fragments[0].row_groups is not None
    assert row_group_fragments[0].row_groups[0].statistics == {
        'f1': {'min': 0, 'max': 1},
        'f2': {'min': 1, 'max': 1},
    }

    fragment = list(dataset.get_fragments(filter=ds.field('f1') < 1))[0]
    row_group_fragments = list(fragment.split_by_row_group(ds.field('f1') < 1))
    assert len(row_group_fragments) == 1
    result = row_group_fragments[0].to_table(filter=ds.field('f1') < 1)
    assert len(result) == 1


@pytest.mark.pandas
@pytest.mark.parquet
def test_fragments_parquet_row_groups_predicate(tempdir):
    table, dataset = _create_dataset_for_fragments(tempdir, chunk_size=2)

    fragment = list(dataset.get_fragments())[0]
    assert fragment.partition_expression.equals(ds.field('part') == 'a')

    # predicate may reference a partition field not present in the
    # physical_schema if an explicit schema is provided to split_by_row_group

    # filter matches partition_expression: all row groups
    row_group_fragments = list(
        fragment.split_by_row_group(ds.field('part') == 'a',
                                    schema=dataset.schema))
    assert len(row_group_fragments) == 2

    # filter contradicts partition_expression: no row groups
    row_group_fragments = list(
        fragment.split_by_row_group(ds.field('part') == 'b',
                                    schema=dataset.schema))
    assert len(row_group_fragments) == 0


@pytest.mark.pandas
@pytest.mark.parquet
def test_fragments_parquet_row_groups_reconstruct(tempdir):
    table, dataset = _create_dataset_for_fragments(tempdir, chunk_size=2)

    fragment = list(dataset.get_fragments())[0]
    parquet_format = fragment.format
    row_group_fragments = list(fragment.split_by_row_group())

    # manually re-construct row group fragments
    new_fragment = parquet_format.make_fragment(
        fragment.path, fragment.filesystem,
        partition_expression=fragment.partition_expression,
        row_groups=[0])
    result = new_fragment.to_table()
    assert result.equals(row_group_fragments[0].to_table())

    # manually re-construct a row group fragment with filter/column projection
    new_fragment = parquet_format.make_fragment(
        fragment.path, fragment.filesystem,
        partition_expression=fragment.partition_expression,
        row_groups={1})
    result = new_fragment.to_table(schema=table.schema, columns=['f1', 'part'],
                                   filter=ds.field('f1') < 3, )
    assert result.column_names == ['f1', 'part']
    assert len(result) == 1

    # out of bounds row group index
    new_fragment = parquet_format.make_fragment(
        fragment.path, fragment.filesystem,
        partition_expression=fragment.partition_expression,
        row_groups={2})
    with pytest.raises(IndexError, match="Trying to scan row group 2"):
        new_fragment.to_table()


def test_partitioning_factory(mockfs):
    paths_or_selector = fs.FileSelector('subdir', recursive=True)
    format = ds.ParquetFileFormat()

    options = ds.FileSystemFactoryOptions('subdir')
    partitioning_factory = ds.DirectoryPartitioning.discover(['group', 'key'])
    assert isinstance(partitioning_factory, ds.PartitioningFactory)
    options.partitioning_factory = partitioning_factory

    factory = ds.FileSystemDatasetFactory(
        mockfs, paths_or_selector, format, options
    )
    inspected_schema = factory.inspect()
    # i64/f64 from data, group/key from "/1/xxx" and "/2/yyy" paths
    expected_schema = pa.schema([
        ("i64", pa.int64()),
        ("f64", pa.float64()),
        ("str", pa.string()),
        ("const", pa.int64()),
        ("group", pa.int32()),
        ("key", pa.string()),
    ])
    assert inspected_schema.equals(expected_schema)

    hive_partitioning_factory = ds.HivePartitioning.discover()
    assert isinstance(hive_partitioning_factory, ds.PartitioningFactory)


def test_partitioning_function():
    schema = pa.schema([("year", pa.int16()), ("month", pa.int8())])
    names = ["year", "month"]

    # default DirectoryPartitioning
    part = ds.partitioning(schema)
    assert isinstance(part, ds.DirectoryPartitioning)
    part = ds.partitioning(field_names=names)
    assert isinstance(part, ds.PartitioningFactory)
    # needs schema or list of names
    with pytest.raises(ValueError):
        ds.partitioning()
    with pytest.raises(ValueError, match="Expected list"):
        ds.partitioning(field_names=schema)
    with pytest.raises(ValueError, match="Cannot specify both"):
        ds.partitioning(schema, field_names=schema)

    # Hive partitioning
    part = ds.partitioning(schema, flavor="hive")
    assert isinstance(part, ds.HivePartitioning)
    part = ds.partitioning(flavor="hive")
    assert isinstance(part, ds.PartitioningFactory)
    # cannot pass list of names
    with pytest.raises(ValueError):
        ds.partitioning(names, flavor="hive")
    with pytest.raises(ValueError, match="Cannot specify 'field_names'"):
        ds.partitioning(field_names=names, flavor="hive")

    # unsupported flavor
    with pytest.raises(ValueError):
        ds.partitioning(schema, flavor="unsupported")


def _create_single_file(base_dir, table=None, row_group_size=None):
    import pyarrow.parquet as pq
    if table is None:
        table = pa.table({'a': range(9), 'b': [0.] * 4 + [1.] * 5})
    path = base_dir / "test.parquet"
    pq.write_table(table, path, row_group_size=row_group_size)
    return table, path


def _create_directory_of_files(base_dir):
    import pyarrow.parquet as pq
    table1 = pa.table({'a': range(9), 'b': [0.] * 4 + [1.] * 5})
    path1 = base_dir / "test1.parquet"
    pq.write_table(table1, path1)
    table2 = pa.table({'a': range(9, 18), 'b': [0.] * 4 + [1.] * 5})
    path2 = base_dir / "test2.parquet"
    pq.write_table(table2, path2)
    return (table1, table2), (path1, path2)


def _check_dataset(dataset, table):
    assert dataset.schema.equals(table.schema)
    assert dataset.to_table().equals(table)


def _check_dataset_from_path(path, table, **kwargs):
    # pathlib object
    assert isinstance(path, pathlib.Path)

    # accept Path, str, List[Path], List[str]
    for p in [path, str(path), [path], [str(path)]]:
        dataset = ds.dataset(path, **kwargs)
        assert isinstance(dataset, ds.FileSystemDataset)
        _check_dataset(dataset, table)

    # relative string path
    with change_cwd(path.parent):
        dataset = ds.dataset(path.name, **kwargs)
        assert isinstance(dataset, ds.FileSystemDataset)
        _check_dataset(dataset, table)


@pytest.mark.parquet
def test_open_dataset_single_file(tempdir):
    table, path = _create_single_file(tempdir)
    _check_dataset_from_path(path, table)


@pytest.mark.parquet
def test_deterministic_row_order(tempdir):
    # ARROW-8447 Ensure that dataset.to_table (and Scanner::ToTable) returns a
    # deterministic row ordering. This is achieved by constructing a single
    # parquet file with one row per RowGroup.
    table, path = _create_single_file(tempdir, row_group_size=1)
    _check_dataset_from_path(path, table)


@pytest.mark.parquet
def test_open_dataset_directory(tempdir):
    tables, _ = _create_directory_of_files(tempdir)
    table = pa.concat_tables(tables)
    _check_dataset_from_path(tempdir, table)


@pytest.mark.parquet
def test_open_dataset_list_of_files(tempdir):
    tables, (path1, path2) = _create_directory_of_files(tempdir)
    table = pa.concat_tables(tables)

    datasets = [
        ds.dataset([path1, path2]),
        ds.dataset([str(path1), str(path2)])
    ]
    for dataset in datasets:
        assert dataset.schema.equals(table.schema)
        result = dataset.to_table()
        assert result.equals(table)


def test_construct_from_single_file(tempdir):
    directory = tempdir / 'single-file'
    directory.mkdir()
    table, path = _create_single_file(directory)
    relative_path = path.relative_to(directory)

    # instantiate from a single file
    d1 = ds.dataset(path)
    # instantiate from a single file with a filesystem object
    d2 = ds.dataset(path, filesystem=fs.LocalFileSystem())
    # instantiate from a single file with prefixed filesystem URI
    d3 = ds.dataset(relative_path, filesystem=_filesystem_uri(directory))
    assert d1.to_table() == d2.to_table() == d3.to_table()


def test_construct_from_single_directory(tempdir):
    directory = tempdir / 'single-directory'
    directory.mkdir()
    tables, paths = _create_directory_of_files(directory)

    d1 = ds.dataset(directory)
    d2 = ds.dataset(directory, filesystem=fs.LocalFileSystem())
    d3 = ds.dataset(directory.name, filesystem=_filesystem_uri(tempdir))
    t1 = d1.to_table()
    t2 = d2.to_table()
    t3 = d3.to_table()
    assert t1 == t2 == t3


def test_construct_from_list_of_files(tempdir):
    # instantiate from a list of files
    directory = tempdir / 'list-of-files'
    directory.mkdir()
    tables, paths = _create_directory_of_files(directory)

    relative_paths = [p.relative_to(tempdir) for p in paths]
    with change_cwd(tempdir):
        d1 = ds.dataset(relative_paths)
        t1 = d1.to_table()
        assert len(t1) == sum(map(len, tables))

    d2 = ds.dataset(relative_paths, filesystem=_filesystem_uri(tempdir))
    t2 = d2.to_table()
    d3 = ds.dataset(paths)
    t3 = d3.to_table()
    d4 = ds.dataset(paths, filesystem=fs.LocalFileSystem())
    t4 = d4.to_table()

    assert t1 == t2 == t3 == t4


def test_construct_from_list_of_mixed_paths_fails(mockfs):
    # isntantiate from a list of mixed paths
    files = [
        'subdir/1/xxx/file0.parquet',
        'subdir/1/xxx/doesnt-exist.parquet',
    ]
    with pytest.raises(FileNotFoundError, match='doesnt-exist'):
        ds.dataset(files, filesystem=mockfs)


def test_construct_from_mixed_child_datasets(mockfs):
    # isntantiate from a list of mixed paths
    dataset = ds.dataset([
        ds.dataset(['subdir/1/xxx/file0.parquet',
                    'subdir/2/yyy/file1.parquet'], filesystem=mockfs),
        ds.dataset('subdir', filesystem=mockfs)
    ])
    assert isinstance(dataset, ds.UnionDataset)
    assert len(list(dataset.get_fragments())) == 4

    table = dataset.to_table()
    assert len(table) == 20
    assert table.num_columns == 4


def test_construct_empty_dataset():
    empty = ds.dataset([])
    table = empty.to_table()
    assert table.num_rows == 0
    assert table.num_columns == 0

    empty = ds.dataset([], schema=pa.schema([
        ('a', pa.int64()),
        ('a', pa.string())
    ]))
    table = empty.to_table()
    assert table.num_rows == 0
    assert table.num_columns == 2


def test_construct_from_invalid_sources_raise(multisourcefs):
    child1 = ds.FileSystemDatasetFactory(
        multisourcefs,
        fs.FileSelector('/plain'),
        format=ds.ParquetFileFormat()
    )
    child2 = ds.FileSystemDatasetFactory(
        multisourcefs,
        fs.FileSelector('/schema'),
        format=ds.ParquetFileFormat()
    )

    with pytest.raises(TypeError, match='Expected.*FileSystemDatasetFactory'):
        ds.dataset([child1, child2])

    expected = (
        "Expected a list of path-like or dataset objects. The given list "
        "contains the following types: int"
    )
    with pytest.raises(TypeError, match=expected):
        ds.dataset([1, 2, 3])

    expected = (
        "Expected a path-like, list of path-likes or a list of Datasets "
        "instead of the given type: NoneType"
    )
    with pytest.raises(TypeError, match=expected):
        ds.dataset(None)


@pytest.mark.parquet
def test_open_dataset_partitioned_directory(tempdir):
    import pyarrow.parquet as pq
    table = pa.table({'a': range(9), 'b': [0.] * 4 + [1.] * 5})

    path = tempdir / "dataset"
    path.mkdir()

    for part in range(3):
        part = path / "part={}".format(part)
        part.mkdir()
        pq.write_table(table, part / "test.parquet")

    # no partitioning specified, just read all individual files
    full_table = pa.concat_tables([table] * 3)
    _check_dataset_from_path(path, full_table)

    # specify partition scheme with discovery
    dataset = ds.dataset(
        str(path), partitioning=ds.partitioning(flavor="hive"))
    expected_schema = table.schema.append(pa.field("part", pa.int32()))
    assert dataset.schema.equals(expected_schema)

    # specify partition scheme with discovery and relative path
    with change_cwd(tempdir):
        dataset = ds.dataset(
            "dataset/", partitioning=ds.partitioning(flavor="hive"))
        expected_schema = table.schema.append(pa.field("part", pa.int32()))
        assert dataset.schema.equals(expected_schema)

    # specify partition scheme with string short-cut
    dataset = ds.dataset(str(path), partitioning="hive")
    assert dataset.schema.equals(expected_schema)

    # specify partition scheme with explicit scheme
    dataset = ds.dataset(
        str(path),
        partitioning=ds.partitioning(
            pa.schema([("part", pa.int8())]), flavor="hive"))
    expected_schema = table.schema.append(pa.field("part", pa.int8()))
    assert dataset.schema.equals(expected_schema)

    result = dataset.to_table()
    expected = full_table.append_column(
        "part", pa.array(np.repeat([0, 1, 2], 9), type=pa.int8()))
    assert result.equals(expected)


@pytest.mark.parquet
def test_open_dataset_filesystem(tempdir):
    # single file
    table, path = _create_single_file(tempdir)

    # filesystem inferred from path
    dataset1 = ds.dataset(str(path))
    assert dataset1.schema.equals(table.schema)

    # filesystem specified
    dataset2 = ds.dataset(str(path), filesystem=fs.LocalFileSystem())
    assert dataset2.schema.equals(table.schema)

    # local filesystem specified with relative path
    with change_cwd(tempdir):
        dataset3 = ds.dataset("test.parquet", filesystem=fs.LocalFileSystem())
    assert dataset3.schema.equals(table.schema)

    # passing different filesystem
    with pytest.raises(FileNotFoundError):
        ds.dataset(str(path), filesystem=fs._MockFileSystem())


@pytest.mark.parquet
def test_open_dataset_unsupported_format(tempdir):
    _, path = _create_single_file(tempdir)
    with pytest.raises(ValueError, match="format 'blabla' is not supported"):
        ds.dataset([path], format="blabla")


@pytest.mark.parquet
def test_open_union_dataset(tempdir):
    _, path = _create_single_file(tempdir)
    dataset = ds.dataset(path)

    union = ds.dataset([dataset, dataset])
    assert isinstance(union, ds.UnionDataset)


def test_open_union_dataset_with_additional_kwargs(multisourcefs):
    child = ds.dataset('/plain', filesystem=multisourcefs, format='parquet')
    with pytest.raises(ValueError, match="cannot pass any additional"):
        ds.dataset([child], format="parquet")


def test_open_dataset_non_existing_file():
    # ARROW-8213: Opening a dataset with a local incorrect path gives confusing
    #             error message
    with pytest.raises(FileNotFoundError):
        ds.dataset('i-am-not-existing.parquet', format='parquet')

    with pytest.raises(pa.ArrowInvalid, match='cannot be relative'):
        ds.dataset('file:i-am-not-existing.parquet', format='parquet')


@pytest.fixture
def s3_example_simple(s3_connection, s3_server):
    from pyarrow.fs import FileSystem
    import pyarrow.parquet as pq

    host, port, access_key, secret_key = s3_connection
    uri = (
        "s3://{}:{}@mybucket/data.parquet?scheme=http&endpoint_override={}:{}"
        .format(access_key, secret_key, host, port)
    )

    fs, path = FileSystem.from_uri(uri)

    fs.create_dir("mybucket")
    table = pa.table({'a': [1, 2, 3]})
    with fs.open_output_stream("mybucket/data.parquet") as out:
        pq.write_table(table, out)

    return table, path, fs, uri, host, port, access_key, secret_key


@pytest.mark.parquet
@pytest.mark.s3
def test_open_dataset_from_uri_s3(s3_example_simple):
    # open dataset from non-localfs string path
    table, path, fs, uri, _, _, _, _ = s3_example_simple

    # full string URI
    dataset = ds.dataset(uri, format="parquet")
    assert dataset.to_table().equals(table)

    # passing filesystem object
    dataset = ds.dataset(path, format="parquet", filesystem=fs)
    assert dataset.to_table().equals(table)


@pytest.mark.parquet
@pytest.mark.s3  # still needed to create the data
def test_open_dataset_from_uri_s3_fsspec(s3_example_simple):
    table, path, _, _, host, port, access_key, secret_key = s3_example_simple
    s3fs = pytest.importorskip("s3fs")

    from pyarrow.fs import PyFileSystem, FSSpecHandler

    fs = s3fs.S3FileSystem(
        key=access_key,
        secret=secret_key,
        client_kwargs={
            'endpoint_url': 'http://{}:{}'.format(host, port)
        }
    )
    fs = PyFileSystem(FSSpecHandler(fs))

    dataset = ds.dataset(path, format="parquet", filesystem=fs)
    assert dataset.to_table().equals(table)


@pytest.mark.parquet
@pytest.mark.s3
def test_open_dataset_from_s3_with_filesystem_uri(s3_connection, s3_server):
    from pyarrow.fs import FileSystem
    import pyarrow.parquet as pq

    host, port, access_key, secret_key = s3_connection
    bucket = 'theirbucket'
    path = 'nested/folder/data.parquet'
    uri = "s3://{}:{}@{}/{}?scheme=http&endpoint_override={}:{}".format(
        access_key, secret_key, bucket, path, host, port
    )

    fs, path = FileSystem.from_uri(uri)
    assert path == 'theirbucket/nested/folder/data.parquet'

    fs.create_dir(bucket)

    table = pa.table({'a': [1, 2, 3]})
    with fs.open_output_stream(path) as out:
        pq.write_table(table, out)

    # full string URI
    dataset = ds.dataset(uri, format="parquet")
    assert dataset.to_table().equals(table)

    # passing filesystem as an uri
    template = (
        "s3://{}:{}@{{}}?scheme=http&endpoint_override={}:{}".format(
            access_key, secret_key, host, port
        )
    )
    cases = [
        ('theirbucket/nested/folder/', '/data.parquet'),
        ('theirbucket/nested/folder', 'data.parquet'),
        ('theirbucket/nested/', 'folder/data.parquet'),
        ('theirbucket/nested', 'folder/data.parquet'),
        ('theirbucket', '/nested/folder/data.parquet'),
        ('theirbucket', 'nested/folder/data.parquet'),
    ]
    for prefix, path in cases:
        uri = template.format(prefix)
        dataset = ds.dataset(path, filesystem=uri, format="parquet")
        assert dataset.to_table().equals(table)

    with pytest.raises(pa.ArrowInvalid, match='Missing bucket name'):
        uri = template.format('/')
        ds.dataset('/theirbucket/nested/folder/data.parquet', filesystem=uri)

    error = (
        "The path component of the filesystem URI must point to a directory "
        "but it has a type: `{}`. The path component is `{}` and the given "
        "filesystem URI is `{}`"
    )

    path = 'theirbucket/doesnt/exist'
    uri = template.format(path)
    with pytest.raises(ValueError) as exc:
        ds.dataset('data.parquet', filesystem=uri)
    assert str(exc.value) == error.format('NotFound', path, uri)

    path = 'theirbucket/nested/folder/data.parquet'
    uri = template.format(path)
    with pytest.raises(ValueError) as exc:
        ds.dataset('data.parquet', filesystem=uri)
    assert str(exc.value) == error.format('File', path, uri)


@pytest.mark.parquet
def test_filter_implicit_cast(tempdir):
    # ARROW-7652
    table = pa.table({'a': pa.array([0, 1, 2, 3, 4, 5], type=pa.int8())})
    _, path = _create_single_file(tempdir, table)
    dataset = ds.dataset(str(path))

    filter_ = ds.field('a') > 2
    assert len(dataset.to_table(filter=filter_)) == 3


def test_dataset_union(multisourcefs):
    child = ds.FileSystemDatasetFactory(
        multisourcefs, fs.FileSelector('/plain'),
        format=ds.ParquetFileFormat()
    )
    factory = ds.UnionDatasetFactory([child])

    # TODO(bkietz) reintroduce factory.children property
    assert len(factory.inspect_schemas()) == 1
    assert all(isinstance(s, pa.Schema) for s in factory.inspect_schemas())
    assert factory.inspect_schemas()[0].equals(child.inspect())
    assert factory.inspect().equals(child.inspect())
    assert isinstance(factory.finish(), ds.Dataset)


def test_union_dataset_from_other_datasets(tempdir, multisourcefs):
    child1 = ds.dataset('/plain', filesystem=multisourcefs, format='parquet')
    child2 = ds.dataset('/schema', filesystem=multisourcefs, format='parquet',
                        partitioning=['week', 'color'])
    child3 = ds.dataset('/hive', filesystem=multisourcefs, format='parquet',
                        partitioning='hive')

    assert child1.schema != child2.schema != child3.schema

    assembled = ds.dataset([child1, child2, child3])
    assert isinstance(assembled, ds.UnionDataset)

    msg = 'cannot pass any additional arguments'
    with pytest.raises(ValueError, match=msg):
        ds.dataset([child1, child2], filesystem=multisourcefs)

    expected_schema = pa.schema([
        ('date', pa.date32()),
        ('index', pa.int64()),
        ('value', pa.float64()),
        ('color', pa.string()),
        ('week', pa.int32()),
        ('year', pa.int32()),
        ('month', pa.int32()),
    ])
    assert assembled.schema.equals(expected_schema)
    assert assembled.to_table().schema.equals(expected_schema)

    assembled = ds.dataset([child1, child3])
    expected_schema = pa.schema([
        ('date', pa.date32()),
        ('index', pa.int64()),
        ('value', pa.float64()),
        ('color', pa.string()),
        ('year', pa.int32()),
        ('month', pa.int32()),
    ])
    assert assembled.schema.equals(expected_schema)
    assert assembled.to_table().schema.equals(expected_schema)

    expected_schema = pa.schema([
        ('month', pa.int32()),
        ('color', pa.string()),
        ('date', pa.date32()),
    ])
    assembled = ds.dataset([child1, child3], schema=expected_schema)
    assert assembled.to_table().schema.equals(expected_schema)

    expected_schema = pa.schema([
        ('month', pa.int32()),
        ('color', pa.string()),
        ('unknown', pa.string())  # fill with nulls
    ])
    assembled = ds.dataset([child1, child3], schema=expected_schema)
    assert assembled.to_table().schema.equals(expected_schema)

    # incompatible schemas, date and index columns have conflicting types
    table = pa.table([range(9), [0.] * 4 + [1.] * 5, 'abcdefghj'],
                     names=['date', 'value', 'index'])
    _, path = _create_single_file(tempdir, table=table)
    child4 = ds.dataset(path)

    with pytest.raises(pa.ArrowInvalid, match='Unable to merge'):
        ds.dataset([child1, child4])


def test_dataset_from_a_list_of_local_directories_raises(multisourcefs):
    msg = 'points to a directory, but only file paths are supported'
    with pytest.raises(IsADirectoryError, match=msg):
        ds.dataset(['/plain', '/schema', '/hive'], filesystem=multisourcefs)


def test_union_dataset_filesystem_datasets(multisourcefs):
    # without partitioning
    dataset = ds.dataset([
        ds.dataset('/plain', filesystem=multisourcefs),
        ds.dataset('/schema', filesystem=multisourcefs),
        ds.dataset('/hive', filesystem=multisourcefs),
    ])
    expected_schema = pa.schema([
        ('date', pa.date32()),
        ('index', pa.int64()),
        ('value', pa.float64()),
        ('color', pa.string()),
    ])
    assert dataset.schema.equals(expected_schema)

    # with hive partitioning for two hive sources
    dataset = ds.dataset([
        ds.dataset('/plain', filesystem=multisourcefs),
        ds.dataset('/schema', filesystem=multisourcefs),
        ds.dataset('/hive', filesystem=multisourcefs, partitioning='hive')
    ])
    expected_schema = pa.schema([
        ('date', pa.date32()),
        ('index', pa.int64()),
        ('value', pa.float64()),
        ('color', pa.string()),
        ('year', pa.int32()),
        ('month', pa.int32()),
    ])
    assert dataset.schema.equals(expected_schema)


@pytest.mark.parquet
def test_specified_schema(tempdir):
    import pyarrow.parquet as pq

    table = pa.table({'a': [1, 2, 3], 'b': [.1, .2, .3]})
    pq.write_table(table, tempdir / "data.parquet")

    def _check_dataset(schema, expected, expected_schema=None):
        dataset = ds.dataset(str(tempdir / "data.parquet"), schema=schema)
        if expected_schema is not None:
            assert dataset.schema.equals(expected_schema)
        else:
            assert dataset.schema.equals(schema)
        result = dataset.to_table()
        assert result.equals(expected)

    # no schema specified
    schema = None
    expected = table
    _check_dataset(schema, expected, expected_schema=table.schema)

    # identical schema specified
    schema = table.schema
    expected = table
    _check_dataset(schema, expected)

    # Specifying schema with change column order
    schema = pa.schema([('b', 'float64'), ('a', 'int64')])
    expected = pa.table([[.1, .2, .3], [1, 2, 3]], names=['b', 'a'])
    _check_dataset(schema, expected)

    # Specifying schema with missing column
    schema = pa.schema([('a', 'int64')])
    expected = pa.table([[1, 2, 3]], names=['a'])
    _check_dataset(schema, expected)

    # Specifying schema with additional column
    schema = pa.schema([('a', 'int64'), ('c', 'int32')])
    expected = pa.table([[1, 2, 3],
                         pa.array([None, None, None], type='int32')],
                        names=['a', 'c'])
    _check_dataset(schema, expected)

    # Specifying with incompatible schema
    schema = pa.schema([('a', 'int32'), ('b', 'float64')])
    dataset = ds.dataset(str(tempdir / "data.parquet"), schema=schema)
    assert dataset.schema.equals(schema)
    with pytest.raises(TypeError):
        dataset.to_table()


def test_ipc_format(tempdir):
    table = pa.table({'a': pa.array([1, 2, 3], type="int8"),
                      'b': pa.array([.1, .2, .3], type="float64")})

    path = str(tempdir / 'test.arrow')
    with pa.output_stream(path) as sink:
        writer = pa.RecordBatchFileWriter(sink, table.schema)
        writer.write_batch(table.to_batches()[0])
        writer.close()

    dataset = ds.dataset(path, format=ds.IpcFileFormat())
    result = dataset.to_table()
    assert result.equals(table)

    for format_str in ["ipc", "arrow"]:
        dataset = ds.dataset(path, format=format_str)
        result = dataset.to_table()
        assert result.equals(table)


@pytest.mark.pandas
def test_csv_format(tempdir):
    table = pa.table({'a': pa.array([1, 2, 3], type="int64"),
                      'b': pa.array([.1, .2, .3], type="float64")})

    path = str(tempdir / 'test.csv')
    table.to_pandas().to_csv(path, index=False)

    dataset = ds.dataset(path, format=ds.CsvFileFormat())
    result = dataset.to_table()
    assert result.equals(table)

    dataset = ds.dataset(path, format='csv')
    result = dataset.to_table()
    assert result.equals(table)


def test_feather_format(tempdir):
    from pyarrow.feather import write_feather

    table = pa.table({'a': pa.array([1, 2, 3], type="int8"),
                      'b': pa.array([.1, .2, .3], type="float64")})

    basedir = tempdir / "feather_dataset"
    basedir.mkdir()
    write_feather(table, str(basedir / "data.feather"))

    dataset = ds.dataset(basedir, format=ds.IpcFileFormat())
    result = dataset.to_table()
    assert result.equals(table)

    dataset = ds.dataset(basedir, format="feather")
    result = dataset.to_table()
    assert result.equals(table)

    # ARROW-8641 - column selection order
    result = dataset.to_table(columns=["b", "a"])
    assert result.column_names == ["b", "a"]
    result = dataset.to_table(columns=["a", "a"])
    assert result.column_names == ["a", "a"]

    # error with Feather v1 files
    write_feather(table, str(basedir / "data1.feather"), version=1)
    with pytest.raises(ValueError):
        ds.dataset(basedir, format="feather").to_table()


def _create_parquet_dataset_simple(root_path):
    import pyarrow.parquet as pq

    metadata_collector = []

    for i in range(4):
        table = pa.table({'f1': [i] * 10, 'f2': np.random.randn(10)})
        pq.write_to_dataset(
            table, str(root_path), metadata_collector=metadata_collector
        )

    metadata_path = str(root_path / '_metadata')
    # write _metadata file
    pq.write_metadata(
        table.schema, metadata_path,
        metadata_collector=metadata_collector
    )
    return metadata_path, table


@pytest.mark.parquet
@pytest.mark.pandas  # write_to_dataset currently requires pandas
def test_parquet_dataset_factory(tempdir):
    root_path = tempdir / "test_parquet_dataset"
    metadata_path, table = _create_parquet_dataset_simple(root_path)
    dataset = ds.parquet_dataset(metadata_path)
    assert dataset.schema.equals(table.schema)
    assert len(dataset.files) == 4
    result = dataset.to_table()
    assert result.num_rows == 40


@pytest.mark.parquet
@pytest.mark.pandas
def test_parquet_dataset_factory_invalid(tempdir):
    root_path = tempdir / "test_parquet_dataset_invalid"
    metadata_path, table = _create_parquet_dataset_simple(root_path)
    # remove one of the files
    list(root_path.glob("*.parquet"))[0].unlink()
    dataset = ds.parquet_dataset(metadata_path)
    assert dataset.schema.equals(table.schema)
    assert len(dataset.files) == 4
    with pytest.raises(FileNotFoundError):
        dataset.to_table()


def _create_metadata_file(root_path):
    # create _metadata file from existing parquet dataset
    import pyarrow.parquet as pq

    parquet_paths = list(sorted(root_path.rglob("*.parquet")))
    schema = pq.ParquetFile(parquet_paths[0]).schema.to_arrow_schema()

    metadata_collector = []
    for path in parquet_paths:
        metadata = pq.ParquetFile(path).metadata
        metadata.set_file_path(str(path.relative_to(root_path)))
        metadata_collector.append(metadata)

    metadata_path = root_path / "_metadata"
    pq.write_metadata(
        schema, metadata_path, metadata_collector=metadata_collector
    )
    return metadata_path


def _create_parquet_dataset_partitioned(root_path):
    import pyarrow.parquet as pq

    table = pa.table([
        pa.array(range(20)), pa.array(np.random.randn(20)),
        pa.array(np.repeat(['a', 'b'], 10))],
        names=["f1", "f2", "part"]
    )
    pq.write_to_dataset(table, str(root_path), partition_cols=['part'])
    return _create_metadata_file(root_path), table


@pytest.mark.parquet
@pytest.mark.pandas
def test_parquet_dataset_factory_partitioned(tempdir):
    root_path = tempdir / "test_parquet_dataset_factory_partitioned"
    metadata_path, table = _create_parquet_dataset_partitioned(root_path)

    partitioning = ds.partitioning(flavor="hive")
    dataset = ds.parquet_dataset(metadata_path, partitioning=partitioning)

    assert dataset.schema.equals(table.schema)
    assert len(dataset.files) == 2
    result = dataset.to_table()
    assert result.num_rows == 20

    # the partitioned dataset does not preserve order
    result = result.to_pandas().sort_values("f1").reset_index(drop=True)
    expected = table.to_pandas()
    pd.testing.assert_frame_equal(result, expected)


@pytest.mark.parquet
@pytest.mark.pandas
def test_parquet_dataset_lazy_filtering(tempdir, open_logging_fs):
    fs, assert_opens = open_logging_fs

    # Test to ensure that no IO happens when filtering a dataset
    # created with ParquetDatasetFactory from a _metadata file

    root_path = tempdir / "test_parquet_dataset_lazy_filtering"
    metadata_path, _ = _create_parquet_dataset_simple(root_path)

    # creating the dataset should only open the metadata file
    with assert_opens([metadata_path]):
        dataset = ds.parquet_dataset(
            metadata_path,
            partitioning=ds.partitioning(flavor="hive"),
            filesystem=fs)

    # materializing fragments should not open any file
    with assert_opens([]):
        fragments = list(dataset.get_fragments())

    # filtering fragments should not open any file
    with assert_opens([]):
        list(dataset.get_fragments(ds.field("f1") > 15))

    # splitting by row group should still not open any file
    with assert_opens([]):
        fragments[0].split_by_row_group(ds.field("f1") > 15)

    # FIXME(bkietz) on Windows this results in FileNotFoundErrors.
    # but actually scanning does open files
    # with assert_opens([f.path for f in fragments]):
    #    dataset.to_table()


@pytest.mark.parquet
@pytest.mark.pandas
def test_dataset_schema_metadata(tempdir):
    # ARROW-8802
    df = pd.DataFrame({'a': [1, 2, 3]})
    path = tempdir / "test.parquet"
    df.to_parquet(path)
    dataset = ds.dataset(path)

    schema = dataset.to_table().schema
    projected_schema = dataset.to_table(columns=["a"]).schema

    # ensure the pandas metadata is included in the schema
    assert b"pandas" in schema.metadata
    # ensure it is still there in a projected schema (with column selection)
    assert schema.equals(projected_schema, check_metadata=True)


@pytest.mark.parquet
def test_filter_mismatching_schema(tempdir):
    # ARROW-9146
    import pyarrow.parquet as pq

    table = pa.table({"col": pa.array([1, 2, 3, 4], type='int32')})
    pq.write_table(table, str(tempdir / "data.parquet"))

    # specifying explicit schema, but that mismatches the schema of the data
    schema = pa.schema([("col", pa.int64())])
    dataset = ds.dataset(
        tempdir / "data.parquet", format="parquet", schema=schema)

    # filtering on a column with such type mismatch should give a proper error
    with pytest.raises(TypeError):
        dataset.to_table(filter=ds.field("col") > 2)

    fragment = list(dataset.get_fragments())[0]
    with pytest.raises(TypeError):
        fragment.to_table(filter=ds.field("col") > 2, schema=schema)
