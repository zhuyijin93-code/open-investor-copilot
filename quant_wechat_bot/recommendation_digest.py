from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
DEFAULT_WEEKLY_TOP_N = 5
DEFAULT_WEEKLY_MONTHS = 6
DEFAULT_SCHEDULE_TIMEZONE = "Asia/Shanghai"
WEEKDAY_ALIASES = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


@dataclasses.dataclass(frozen=True)
class DigestResult:
    template: str
    text: str
    session_date: dt.date
    market_label: str
    top_n: int
    lookback_months: int | None = None


@dataclasses.dataclass(frozen=True)
class TemplateScheduleConfig:
    enabled: bool
    market: str
    top_n: int
    lookback_months: int | None
    weekdays: tuple[int, ...]


@dataclasses.dataclass(frozen=True)
class RecommendationDigestConfig:
    schedule_timezone: str
    daily_recommendation: TemplateScheduleConfig
    weekly_review: TemplateScheduleConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and send periodic stock recommendation digests.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Generate the latest stock recommendation digest and print it.")
    send_parser = subparsers.add_parser("send", help="Generate and send the latest stock recommendation digest.")

    for subparser in (build_parser, send_parser):
        subparser.add_argument(
            "--template",
            choices=("daily", "weekly"),
            default="daily",
            help="Digest template. `daily` sends 推荐日报, `weekly` sends 周复盘 + 下周候选池.",
        )
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
        subparser.add_argument(
            "--months",
            dest="lookback_months",
            type=int,
            default=None,
            help="Weekly review lookback months. Only used with --template weekly.",
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
    schedule_parser = subparsers.add_parser("run-schedule", help="Send whichever digest templates are due today.")
    schedule_parser.add_argument(
        "--state-path",
        default=str(STATE_PATH),
        help=f"State file for dedupe. Default: {STATE_PATH}.",
    )
    schedule_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve the send chain but do not actually deliver to WeChat.",
    )
    schedule_parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore dedupe state and send even if the same session has already been handled.",
    )
    schedule_parser.add_argument(
        "--quiet-skip",
        action="store_true",
        help="Exit quietly when there is no scheduled digest to send today.",
    )
    return parser.parse_args()


def normalize_weekdays(value: object, default: tuple[int, ...]) -> tuple[int, ...]:
    if isinstance(value, list):
        days: list[int] = []
        for item in value:
            if isinstance(item, int) and 0 <= item <= 6:
                days.append(item)
                continue
            if isinstance(item, str):
                resolved = WEEKDAY_ALIASES.get(item.strip().lower())
                if resolved is not None:
                    days.append(resolved)
        if days:
            return tuple(dict.fromkeys(days))
    return default


def load_template_schedule(
    payload: object,
    *,
    default_market: str,
    default_top_n: int,
    default_lookback_months: int | None,
    default_weekdays: tuple[int, ...],
) -> TemplateScheduleConfig:
    enabled = True
    market = default_market
    top_n = default_top_n
    lookback_months = default_lookback_months
    weekdays = default_weekdays
    if isinstance(payload, dict):
        raw_enabled = payload.get("enabled")
        if isinstance(raw_enabled, bool):
            enabled = raw_enabled
        raw_market = payload.get("market")
        if isinstance(raw_market, str) and raw_market.strip():
            market = raw_market.strip()
        raw_top_n = payload.get("top_n")
        if isinstance(raw_top_n, int) and raw_top_n > 0:
            top_n = raw_top_n
        raw_months = payload.get("lookback_months")
        if isinstance(raw_months, int) and raw_months > 0:
            lookback_months = raw_months
        weekdays = normalize_weekdays(payload.get("weekdays"), default_weekdays)
    return TemplateScheduleConfig(
        enabled=enabled,
        market=market,
        top_n=top_n,
        lookback_months=lookback_months,
        weekdays=weekdays,
    )


def load_recommendation_config() -> RecommendationDigestConfig:
    settings = market_close_digest.load_settings()
    payload = settings.get("recommendation_digest") if isinstance(settings, dict) else None
    default_market = DEFAULT_MARKET
    default_top_n = DEFAULT_TOP_N
    schedule_timezone = DEFAULT_SCHEDULE_TIMEZONE
    if isinstance(payload, dict):
        raw_market = payload.get("default_market")
        raw_top_n = payload.get("top_n")
        raw_timezone = payload.get("schedule_timezone")
        if isinstance(raw_market, str) and raw_market.strip():
            default_market = raw_market.strip()
        if isinstance(raw_top_n, int) and raw_top_n > 0:
            default_top_n = raw_top_n
        if isinstance(raw_timezone, str) and raw_timezone.strip():
            schedule_timezone = raw_timezone.strip()
    daily_recommendation = load_template_schedule(
        payload.get("daily_recommendation") if isinstance(payload, dict) else None,
        default_market=default_market,
        default_top_n=default_top_n,
        default_lookback_months=None,
        default_weekdays=(0, 1, 2, 3, 4),
    )
    weekly_review = load_template_schedule(
        payload.get("weekly_review") if isinstance(payload, dict) else None,
        default_market=default_market,
        default_top_n=DEFAULT_WEEKLY_TOP_N,
        default_lookback_months=DEFAULT_WEEKLY_MONTHS,
        default_weekdays=(5, 6),
    )
    return RecommendationDigestConfig(
        schedule_timezone=schedule_timezone,
        daily_recommendation=daily_recommendation,
        weekly_review=weekly_review,
    )


def schedule_config_for_template(config: RecommendationDigestConfig, template: str) -> TemplateScheduleConfig:
    if template == "weekly":
        return config.weekly_review
    return config.daily_recommendation


def resolve_market(value: str | None, *, template: str) -> str:
    if value is not None and value.strip():
        return value.strip()
    config = load_recommendation_config()
    return schedule_config_for_template(config, template).market


def resolve_top_n(value: int | None, *, template: str) -> int:
    if value is not None and value > 0:
        return value
    config = load_recommendation_config()
    return schedule_config_for_template(config, template).top_n


def resolve_lookback_months(value: int | None, *, template: str) -> int | None:
    if template != "weekly":
        return None
    if value is not None and value > 0:
        return value
    config = load_recommendation_config()
    return schedule_config_for_template(config, template).lookback_months


def digest_bucket_key(
    market: str,
    top_n: int,
    *,
    template: str = "daily",
    lookback_months: int | None = None,
) -> str:
    suffix = f"_m{lookback_months}" if template == "weekly" and lookback_months is not None else ""
    return f"{template}_{trend_strategy.market_label(market)}_top{top_n}{suffix}"


def already_sent(
    path: Path,
    market: str,
    top_n: int,
    session_date: dt.date,
    *,
    template: str = "daily",
    lookback_months: int | None = None,
) -> bool:
    state = market_close_digest.load_state(path)
    bucket = digest_bucket_key(market, top_n, template=template, lookback_months=lookback_months)
    return session_date.isoformat() in state.get(bucket, {})


def mark_sent(
    path: Path,
    market: str,
    top_n: int,
    session_date: dt.date,
    digest_text: str,
    *,
    template: str = "daily",
    lookback_months: int | None = None,
) -> None:
    state = market_close_digest.load_state(path)
    bucket = state.setdefault(
        digest_bucket_key(market, top_n, template=template, lookback_months=lookback_months),
        {},
    )
    bucket[session_date.isoformat()] = {
        "sent_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preview": digest_text[:200],
    }
    market_close_digest.save_state(path, state)


def build_recommendation_digest(
    market: str | None = None,
    top_n: int | None = None,
    *,
    template: str = "daily",
    lookback_months: int | None = None,
) -> DigestResult:
    effective_market = resolve_market(market, template=template)
    effective_top_n = resolve_top_n(top_n, template=template)
    universe_path = bot_service.resolve_trend_universe_path(effective_market)
    if template == "weekly":
        effective_months = resolve_lookback_months(lookback_months, template=template) or DEFAULT_WEEKLY_MONTHS
        report = trend_strategy.backtest_trend_strategy(
            universe_path,
            effective_market,
            lookback_months=effective_months,
            top_n=effective_top_n,
        )
        return DigestResult(
            template=template,
            text=trend_strategy.format_weekly_review_from_report(report),
            session_date=report.end_date,
            market_label=report.market_label,
            top_n=effective_top_n,
            lookback_months=effective_months,
        )
    snapshot = trend_strategy.build_trend_snapshot(universe_path, effective_market, top_n=effective_top_n)
    return DigestResult(
        template=template,
        text=trend_strategy.format_recommendation_digest_from_snapshot(snapshot),
        session_date=trend_strategy.snapshot_session_date(snapshot),
        market_label=trend_strategy.market_label(effective_market),
        top_n=effective_top_n,
    )


def run_build(args: argparse.Namespace) -> int:
    result = build_recommendation_digest(
        args.market,
        args.top_n,
        template=args.template,
        lookback_months=args.lookback_months,
    )
    if args.output:
        market_close_digest.write_output(Path(args.output).expanduser(), result.text)
    sys.stdout.write(result.text + "\n")
    return 0


def send_digest(
    result: DigestResult,
    *,
    state_path: Path,
    dry_run: bool,
    force: bool,
    quiet_skip: bool,
) -> int:
    template_label = "周复盘" if result.template == "weekly" else "推荐日报"
    if not force and already_sent(
        state_path,
        result.market_label,
        result.top_n,
        result.session_date,
        template=result.template,
        lookback_months=result.lookback_months,
    ):
        if not quiet_skip:
            print(f"{result.market_label} {result.session_date.isoformat()} 的{template_label}已经发送过，跳过。")
        return 0
    market_close_digest.send_wechat_message(result.text, dry_run=dry_run)
    if not dry_run:
        mark_sent(
            state_path,
            result.market_label,
            result.top_n,
            result.session_date,
            result.text,
            template=result.template,
            lookback_months=result.lookback_months,
        )
    extra = f" | {result.lookback_months}m" if result.template == "weekly" and result.lookback_months is not None else ""
    print(f"{result.market_label} {template_label}已处理: {result.session_date.isoformat()} (top {result.top_n}{extra})")
    return 0


def run_send(args: argparse.Namespace) -> int:
    state_path = Path(args.state_path).expanduser()
    result = build_recommendation_digest(
        args.market,
        args.top_n,
        template=args.template,
        lookback_months=args.lookback_months,
    )
    return send_digest(
        result,
        state_path=state_path,
        dry_run=args.dry_run,
        force=args.force,
        quiet_skip=args.quiet_skip,
    )


def scheduled_templates_for_weekday(
    config: RecommendationDigestConfig,
    weekday: int,
) -> tuple[tuple[str, TemplateScheduleConfig], ...]:
    selected: list[tuple[str, TemplateScheduleConfig]] = []
    if config.daily_recommendation.enabled and weekday in config.daily_recommendation.weekdays:
        selected.append(("daily", config.daily_recommendation))
    if config.weekly_review.enabled and weekday in config.weekly_review.weekdays:
        selected.append(("weekly", config.weekly_review))
    return tuple(selected)


def run_schedule(args: argparse.Namespace) -> int:
    config = load_recommendation_config()
    now_value = dt.datetime.now(ZoneInfo(config.schedule_timezone))
    scheduled = scheduled_templates_for_weekday(config, now_value.weekday())
    if not scheduled:
        if not args.quiet_skip:
            print(f"{now_value.date().isoformat()} 没有命中的定时模板。")
        return 0
    state_path = Path(args.state_path).expanduser()
    for template, template_config in scheduled:
        result = build_recommendation_digest(
            template_config.market,
            template_config.top_n,
            template=template,
            lookback_months=template_config.lookback_months,
        )
        send_digest(
            result,
            state_path=state_path,
            dry_run=args.dry_run,
            force=args.force,
            quiet_skip=args.quiet_skip,
        )
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.command == "build":
            return run_build(args)
        if args.command == "send":
            return run_send(args)
        if args.command == "run-schedule":
            return run_schedule(args)
        raise RuntimeError(f"Unsupported command: {args.command}")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
