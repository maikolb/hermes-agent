from scripts.nfos_state_maintenance import build_parser


def test_nfos_maintenance_surface_has_no_delete_argument():
    parser = build_parser()
    help_text = parser.format_help()
    assert "archive-copy" in help_text
    assert "delete" not in help_text.lower()


def test_nfos_archive_requires_fresh_candidate_limit():
    parser = build_parser()
    try:
        parser.parse_args(
            [
                "archive-copy",
                "--db",
                "C:/state.db",
                "--output",
                "C:/archive.db",
                "--older-than-days",
                "90",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("archive-copy accepted no candidate limit")
