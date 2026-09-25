from pathlib import Path


def test_runtime_has_no_v2_dependency_except_hostname_and_historical_adapter():
    root = Path(__file__).parents[2]
    sources = [
        root / "s3tables_uploader" / "job_store.py",
        root / "infra" / "s3_uploader_fargate.py",
    ]
    occurrences = {
        source.relative_to(root).as_posix(): [line.strip() for line in source.read_text().splitlines() if "s3-uploader-v2" in line]
        for source in sources
    }
    assert occurrences == {
        "s3tables_uploader/job_store.py": ['HISTORICAL_LANDING_PREFIX = "s3-uploader-v2"'],
        "infra/s3_uploader_fargate.py": ['DOMAIN = "s3-uploader-v2.bot-alex.com"  # Retained public hostname.', 'HISTORICAL_PREFIX = "s3-uploader-v2"'],
    }
