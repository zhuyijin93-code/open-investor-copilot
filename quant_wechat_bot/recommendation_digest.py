from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import sys
from pathlib import Path
from typing import Any

try:
    from . import bot_service, market_close_digest, trend_strategy
except ImportError:  # pragma: no cover - allows direct script execution
    import bot_service  # type: ignore
    import market_close_digest  # type: ignore
    import trend_strategy  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parent
STATE_PATH = PROJECT_ROOT / ".cache" / "recommendation_digest_state.json"
DEFAULT_MARKET = "全市场"
DEFAULT_TOP_N = 3


@dataclasses.dataclass(frozen=True)
class RecommendationDigestConfig:
    default_market: str
    top_n: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and send periodic stock recommendation digests.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Generate the latest stock recommendation digest and print it.")
    send_parser = subparsers.add_parser("send", help="Generate and send the latest stock recommendation digest.")

    for subparser in (build_parser, send_parser):
        subparser.add_argument(
            "--market",
            default=None,
            help="Market label, for example 全市场 / A股 / 港股 / 美股. Defaults to recommendation_digest.default_market.",
        )
        subparser.add_argument(
            "--top-n",
            dest="top_n",
            type=int,
            default=None,
            help="Number of top trend names to consider. Defaults to recommendation_digest.top_n.",
        )

    build_parser.add_argument("--output", help="Optional file to write the digest to.")

    send_parser.add_argument(
        "--state-path",
        default=str(STATE_PATH),
        help=f"State file for dedupe. Default: {STATE_PATH}.",
    )
    send_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve the send chain but do not actually deliver to WeChat.",
    )
    send_parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore dedupe state and send even if the same session has already been handled.",
    )
    send_parser.add_argument(
        "--quiet-skip",
        action="store_true",
        help="Exit quietly when there is no new digest to send.",
    )
    return parser.parse_args()


def load_recommendation_config() -> RecommendationDigestConfig:
    settings = market_close_digest.load_settings()
    payload = settings.get("recommendation_digest") if isinstance(settings, dict) else None
    default_market = DEFAULT_MARKET
    top_n = DEFAULT_TOP_N
    if isinstance(payload, dict):
        raw_market = payload.get("default_market")
        raw_top_n = payload.get("top_n")
        if isinstance(raw_market, str) and raw_market.strip():
            default_market = raw_market.strip()
        if isinstance(raw_top_n, int) and raw_top_n > 0:
            top_n = raw_top_n
    return RecommendationDigestConfig(default_market=default_market, top_n=top_n)


def resolve_market(value: str | None) -> str:
    if value is not None and value.strip():
        return value.strip()
    return load_recommendation_config().default_market


def resolve_top_n(value: int | None) -> int:
    if value is not None and value > 0:
        return value
    return load_recommendation_config().top_n


def digest_bucket_key(market: str, top_n: int) -> str:
    return f"{trend_strategy.market_label(market)}_top{top_n}"


def already_sent(path: Path, market: str, top_n: int, session_date: dt.date) -> bool:
    state = market_close_digest.load_state(path)
    return session_date.isoformat() in state.get(digest_bucket_key(market, top_n), {})


def mark_sent(path: Path, market: str, top_n: int, session_date: dt.date, digest_text: str) -> None:
    state = market_close_digest.load_state(path)
    bucket = state.setdefault(digest_bucket_key(market, top_n), {})
    bucket[session_date.isoformat()] = {
        "sent_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preview": digest_text[:200],
    }
    market_close_digest.save_state(path, state)


def build_recommendation_digest(market: str | None = None, top_n: int | None = None) -> tuple[str, dt.date, str, int]:
    effective_market = resolve_market(market)
    effective_top_n = resolve_top_n(top_n)
    universe_path = bot_service.resolve_trend_universe_path(effective_market)
    snapshot = trend_strategy.build_trend_snapshot(universe_path, effective_market, top_n=effective_top_n)
    digest = trend_strategy.format_recommendation_digest_from_snapshot(snapshot)
    session_date = trend_strategy.snapshot_session_date(snapshot)
    return digest, session_date, trend_strategy.market_label(effective_market), effective_top_n


def run_build(args: argparse.Namespace) -> int:
    digest, _, _, _ = build_recommendation_digest(args.market, args.top_n)
    if args.output:
        market_close_digest.write_output(Path(args.output).expanduser(), digest)
    sys.stdout.write(digest + "\n")
    return 0


def run_send(args: argparse.Namespace) -> int:
    state_path = Path(args.state_path).expanduser()
    digest, session_date, market_label, top_n = build_recommendation_digest(args.market, args.top_n)
    if not args.force and already_sent(state_path, market_label, top_n, session_date):
        if not args.quiet_skip:
            print(f"{market_label} {session_date.isoformat()} 的推荐日报已经发送过，跳过。")
        return 0
    market_close_digest.send_wechat_message(digest, dry_run=args.dry_run)
    if not args.dry_run:
        mark_sent(state_path, market_label, top_n, session_date, digest)
    print(f"{market_label} 推荐日报已处理: {session_date.isoformat()} (top {top_n})")
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.command == "build":
            return run_build(args)
        if args.command == "send":
            return run_send(args)
        raise RuntimeError(f"Unsupported command: {args.command}")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
