"""Parquet dataset backed block header storage."""

import logging
from pathlib import Path
from typing import Tuple, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
from pyarrow._dataset import FilenamePartitioning

from .block_header_store import BlockHeaderStore


logger = logging.getLogger(__name__)


class NoGapsWritten(Exception):
    """Do not allow gaps in data."""


class ParquetDatasetBlockHeaderStore(BlockHeaderStore):
    """Store block headers as Parquet dataset.

    - Partitions are keyed by block number

    - Partitioning allows fast incremental updates,
      by overwriting the last two partitions
    """

    def __init__(self,
                 path: Path,
                 partition_size=100_000,
                 ):
        """

        :param path:
            Directory and a metadata file there

        :param partition_size:
        """
        assert isinstance(path, Path)
        self.path = path
        self.partition_size = partition_size

        part_scheme = FilenamePartitioning(
            pa.schema([("partition", pa.uint32())])
        )

        self.partitioning = part_scheme

    def floor_block_number_to_partition_start(self, n) -> int:
        block_num = n // self.partition_size * self.partition_size
        if block_num == 0:
            return 1
        return block_num

    def load(self, since_block_number: int = 0) -> pd.DataFrame:
        """Load data from parquet.

        :param since_block_number:
            May return earlier rows than this if a block is a middle of a partition
        """
        #dataset = ds.parquet_dataset(self.path, partitioning=self.partitioning)
        dataset = ds.dataset(self.path, partitioning=self.partitioning)
        partition_start_block = self.floor_block_number_to_partition_start(since_block_number)
        # Load data only from the partitions we need
        filtered_table = dataset.to_table(filter=ds.field('partition') >= partition_start_block)
        df = filtered_table.to_pandas()
        return df

    def save(self, df: pd.DataFrame, since_block_number: int = 0):
        """Savea all data from parquet.

        If there are existing block headers written, any data will be overwritten
        on per partition basis.

        :param since_block_number:
            Write only the latest data after this block number (inclusive)
        """

        assert "partition" in df.columns

        if since_block_number:
            df = df.loc[df.block_number >= since_block_number]

        table = pa.Table.from_pandas(df)
        ds.write_dataset(table,
                         self.path,
                         format="parquet",
                         partitioning=self.partitioning,
                         existing_data_behavior="overwrite_or_ignore",
                         use_threads=False,
                         )

    def save_incremental(self, df: pd.DataFrame) -> Tuple[int, int]:
        """Write all partitions we are missing from the data.

        - We need to write minimum two partitions

        - There might be gaps in data we can write

        - There might be gaps data on disk we have already written

        - Do some heurestics what to write
        """

        last_written_block = self.peak_last_block()
        if last_written_block:
            last_written_partition_starts_at = self.floor_block_number_to_partition_start(last_written_block)
        else:
            last_written_partition_starts_at = 1

        last_block_number_data_has = df.iloc[-1]["block_number"]

        minimum_partitioned_block_writer_needs = self.floor_block_number_to_partition_start(last_block_number_data_has) - self.partition_size
        minimum_partitioned_block_writer_needs = max(1, minimum_partitioned_block_writer_needs)

        write_starts_at = min(minimum_partitioned_block_writer_needs, last_written_partition_starts_at)

        logger.info("Writing %s. In-memory data len: %d. Last block written before: %s. Last block in-mem data has: %d. Write starts: %d", self.path, len(df), last_written_block, last_block_number_data_has, write_starts_at)

        self.save(df, write_starts_at)
        return write_starts_at, last_block_number_data_has

    def peak_last_block(self) -> Optional[int]:
        """Return the last block number stored on the disk."""
        dataset = ds.dataset(self.path, partitioning=self.partitioning)
        fragments = list(dataset.get_fragments())

        if not fragments:
            return None

        last_fragment = fragments[-1]
        # TODO: How to select last row with pyarrow
        df = last_fragment.to_table().to_pandas()
        return df.iloc[-1]["block_number"]



