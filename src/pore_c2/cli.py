from contextlib import closing
from itertools import islice
from pathlib import Path
from typing import Optional

import mappy
import typer
from numpy.random import default_rng
from pysam import FastaFile, faidx  # pyright: ignore [reportGeneralTypeIssues]

from pore_c2 import __version__

from .aligns import annotate_monomer_alignments
from .index import IndexFileCollection, IndexMetadata
from .io import (
    find_files,
    get_alignment_header,
    get_concatemer_seqs,
    get_monomer_aligns,
    get_monomer_writer,
)
from .log import get_logger, init_logger
from .model import EnzymeCutter
from .monomers import digest_genome
from .settings import MINIMAP2_SETTINGS
from .testing import Scenario

# from rich.console import Console


app = typer.Typer(pretty_exceptions_enable=False)
# console = Console()


@app.callback()
def main(quiet: bool = False, logfile: Optional[Path] = None):
    init_logger(quiet=quiet, logfile=logfile)


@app.command()
def index(fasta: Path, enzyme: str, prefix: Optional[Path] = None, force: bool = False):
    logger = get_logger()
    try:
        cutter = EnzymeCutter.from_name(enzyme)
    except Exception:
        logger.error(f"Error loading enzyme {enzyme}", exc_info=True)
        raise
    if prefix is None:
        prefix = fasta.parent / f"{fasta.stem}.porec.{enzyme}"
    index_files = IndexFileCollection.with_prefix(prefix)
    if index_files.exists_any() and not force:
        logger.error(
            "Some of the outputs already exist, please remove before continuing"
        )
        raise IOError
    idx_path = Path(str(fasta) + ".fai")
    if not idx_path.exists():
        logger.info(f"Creating a .fai for {fasta}")
        faidx(str(fasta))
    df = digest_genome(
        cutter=cutter,
        fasta=fasta,
        bed_file=index_files.bed,
        fasta_out=index_files.fasta,
    )
    if index_files.fragments:
        df.write_parquet(index_files.fragments)
        logger.info(f"Wrote {len(df)} fragments to {index_files.fragments}")
    logger.debug(
        f"Creating minimap index of {fasta} at {index_files.mmi} "
        f"using preset '{MINIMAP2_SETTINGS}'"
    )
    mappy.Aligner(
        fn_idx_in=str(fasta), fn_idx_out=str(index_files.mmi), **MINIMAP2_SETTINGS
    )
    ff = FastaFile(str(fasta))
    metadata = IndexMetadata(
        enzyme=enzyme,
        reference_path=str(fasta.absolute()),
        chrom_order=list(ff.references),
        chrom_lengths={c: ff.get_reference_length(c) for c in ff.references},
        pore_c_version=__version__,
        mappy_settings=MINIMAP2_SETTINGS,
    )
    index_files.save_metadata(metadata)
    logger.debug(index_files.metadata.read_text())
    return index_files


@app.command()
def align():
    raise NotImplementedError


@app.command()
def merge():
    raise NotImplementedError


utils = typer.Typer()


@utils.command()
def digest_concatemers(
    file_or_root: Path,
    enzyme: str,
    output_path: Path,
    glob: str = "*.fastq",
    recursive: bool = True,
    max_reads: int = 0,
):

    logger = get_logger()
    logger.info("Digesting concatemers")
    input_files = list(find_files(file_or_root, glob=glob, recursive=recursive))
    header = get_alignment_header(source_files=input_files)
    read_stream = get_concatemer_seqs(input_files)
    if max_reads:
        read_stream = islice(read_stream, max_reads)
    cutter = EnzymeCutter.from_name(enzyme)
    monomer_stream = (
        monomer.read_seq for read in read_stream for monomer in read.cut(cutter)
    )

    with closing(get_monomer_writer(output_path, header=header)) as writer:
        writer.consume(monomer_stream)
    # logger.info(
    #    f"Wrote {writer.base_counter:,} bases in "
    #    f"{writer.read_counter:,} reads to {output_path}"
    # )
    return writer


@utils.command()
def create_test_data(
    base_dir: Path,
    genome_size: int = 5_000,
    num_chroms: int = 2,
    cut_rate: float = 0.005,
    enzyme: str = "NlaIII",
    num_concatemers: int = 100,
    num_haplotypes: int = 0,
    variant_density: float = 0.0,
    seed: Optional[int] = None,
):
    logger = get_logger()
    logger.info(f"Creating test data at: {base_dir}")

    if seed is None:
        rng = default_rng()
    else:
        rng = default_rng(seed)
    logger.debug(f"Dividing genome {genome_size} into {num_chroms} chromosomes")
    chrom_lengths = {
        f"chr{x+1}": v
        for x, v in enumerate(
            sorted(rng.choice(genome_size, size=num_chroms, replace=False))
        )
    }
    logger.debug("Creating scenario")
    scenario = Scenario(
        chrom_lengths,
        cut_rate=cut_rate,
        enzyme=enzyme,
        num_concatemers=num_concatemers,
        num_haplotypes=num_haplotypes,
        variant_density=variant_density,
        temp_path=base_dir,
        random_state=rng,
    )
    logger.info(f"Creating scenario: {scenario}")
    logger.info(f"Genome fasta: {scenario.reference_fasta}")
    logger.info(f"Concatemer fastq: {scenario.concatemer_fastq}")
    if num_haplotypes >= 2 and variant_density > 0:
        logger.info(f"Phased VCF: {scenario.concatemer_fastq}")
    return scenario


@utils.command()
def process_monomer_alignments(bam: Path, output_path: Path):
    logger = get_logger()
    logger.info(f"Processing reads from {bam}")
    input_files = [bam]
    header = get_alignment_header(source_files=input_files)
    writer = get_monomer_writer(output_path, header=header)

    monomer_aligns = get_monomer_aligns(input_files)
    annotated_stream = (
        m.read_seq
        for (_, monomers) in annotate_monomer_alignments(monomer_aligns)
        for m in monomers
    )

    with closing(get_monomer_writer(output_path, header=header)) as writer:
        writer.consume(annotated_stream)
    # logger.info(
    #    f"Wrote {writer.base_counter:,} bases in "
    #    f"{writer.read_counter:,} reads to {output_path}"
    # )
    return writer


@utils.command()
def add_phase_info(
    bam: Path, vcf: Path, output_path: Path, reference: Optional[Path] = None
):
    logger = get_logger()
    logger.info(f"Processing reads from {bam}")
    from pore_c2.variants import get_phased_alignments

    for a in get_phased_alignments(bam=bam, vcf=vcf, reference=reference):
        pass


app.add_typer(utils, name="utils")

if __name__ == "__main__":
    app()
