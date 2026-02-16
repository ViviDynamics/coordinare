from __future__ import annotations

from coordinare.__main__ import _build_arg_parser


def test_parser_supports_structured_output_flag() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args(["--structured-output"])
    assert args.structured_output is True


def test_parser_supports_log_level_override() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args(["--log-level", "debug"])
    assert args.log_level == "debug"
