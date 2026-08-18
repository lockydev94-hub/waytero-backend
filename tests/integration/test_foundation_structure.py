from pathlib import Path


def test_phase_zero_scaffold_files_exist() -> None:
    repo_root = Path(__file__).resolve().parents[3]

    required_paths = [
        repo_root / "Backend" / "pytest.ini",
        repo_root / "Frontend" / "admin-portal" / "package.json",
        repo_root / "Frontend" / "partner-portal" / "package.json",
        repo_root / "Frontend" / "customer-web" / "package.json",
        repo_root / "Frontend" / "shared-ui" / "package.json",
        repo_root / "Mobile" / "customer_app" / "pubspec.yaml",
        repo_root / "Mobile" / "captain_app" / "pubspec.yaml",
        repo_root / "Mobile" / "verification_app" / "pubspec.yaml",
        repo_root / "Tests" / "README.md",
    ]

    for required_path in required_paths:
        assert required_path.exists(), f"Missing scaffold file: {required_path}"
