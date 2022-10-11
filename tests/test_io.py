from pathlib import Path

import pytest
from pysam import fqimport

from pore_c2.io import find_files, iter_reads


@pytest.mark.parametrize(
    "layout,glob,recursive,expected",
    [
        (["A/a/a.fastq"], None, None, None),
        (["a.fastq"], None, None, None),
        (["a.fastq.gz"], None, None, []),
        (["a.fastq", "A/a/a.fastq"], None, False, ["a.fastq"]),
    ],
)
def test_find_files(tmp_path, layout, glob, recursive, expected):
    root = Path(str(tmp_path))
    if expected is None:
        expected = layout
    kwds = {}
    if glob is not None:
        kwds["glob"] = glob
    if recursive is not None:
        kwds["recursive"] = recursive
    for _ in layout:
        p = root / _
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_)
    res = list(
        [str(_).replace(str(tmp_path), "")[1:] for _ in find_files(root, **kwds)]
    )
    assert res == expected


@pytest.fixture
def mock_fastq(tmp_path, mock_reads):
    outfile = tmp_path / "test.fastq"
    written = []
    with outfile.open("w") as fh:
        for r in mock_reads.values():
            if r.name != "read_no_qual":
                fh.write(r.to_fastq_str())
                written.append(r)
        fh.flush()
    return outfile


def test_fastq_reader(mock_fastq, mock_reads):
    written = [v for k, v in mock_reads.items() if k != "read_no_qual"]
    reads = list(iter_reads(mock_fastq))
    assert len(reads) == len(written)


def test_ubam_reader(mock_fastq, mock_reads):
    ubam = mock_fastq.with_suffix(".bam")
    written = [v for k, v in mock_reads.items() if k != "read_no_qual"]
    fqimport("-o", str(ubam), "-T", "*", str(mock_fastq))
    reads = list(iter_reads(ubam))
    assert len(reads) == len(written)