import json
from enum import StrEnum
from typing import Annotated, cast

import typer

from google_keyword_ai.config import load_settings
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.expansion import ExpansionStrategy
from google_keyword_ai.logging import configure_logging
from google_keyword_ai.pipeline.budget import Budget
from google_keyword_ai.usecases.ads import run_ads_historical, run_ads_ideas, run_competitor
from google_keyword_ai.usecases.doctor import run_config_show, run_doctor
from google_keyword_ai.usecases.expand import run_expand
from google_keyword_ai.usecases.gsc import (
    run_gsc_opportunities,
    run_gsc_properties,
    run_gsc_queries,
)
from google_keyword_ai.usecases.research import run_research
from google_keyword_ai.usecases.suggest import run_suggest
from google_keyword_ai.usecases.trends import run_trends, run_trends_compare

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
config_app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
ads_app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
gsc_app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
app.add_typer(config_app, name="config")
app.add_typer(ads_app, name="ads")
app.add_typer(gsc_app, name="gsc")


class OutputFormat(StrEnum):
    JSON = "json"
    TABLE = "table"


def _print_envelope[T](envelope: Envelope[T], output_format: OutputFormat) -> None:
    wire = envelope.to_wire()
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(wire, ensure_ascii=False))
        return

    typer.echo("FIELD\tVALUE")
    for name, value in wire.items():
        rendered = (
            json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
        )
        typer.echo(f"{name}\t{rendered}")


def _finish[T](envelope: Envelope[T], output_format: OutputFormat) -> None:
    _print_envelope(envelope, output_format)
    if envelope.completeness is not Completeness.COMPLETE:
        raise typer.Exit(code=1)


@app.command()
def doctor(
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.JSON,
) -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    _finish(run_doctor(settings), output_format)


@config_app.command("show")
def config_show(
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.JSON,
) -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    _finish(run_config_show(settings), output_format)


@app.command()
def suggest(
    query: str,
    language: Annotated[str | None, typer.Option("--language")] = None,
    country: Annotated[str | None, typer.Option("--country")] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.JSON,
) -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    _finish(
        run_suggest(
            settings,
            query,
            language=language,
            country=country,
            limit=limit,
        ),
        output_format,
    )


@app.command()
def expand(
    seed: str,
    language: Annotated[str | None, typer.Option("--language")] = None,
    country: Annotated[str | None, typer.Option("--country")] = None,
    depth: Annotated[int | None, typer.Option("--depth")] = None,
    max_queries: Annotated[int | None, typer.Option("--max-queries")] = None,
    max_results: Annotated[int | None, typer.Option("--max-results")] = None,
    max_runtime_seconds: Annotated[float | None, typer.Option("--max-runtime")] = None,
    strategies: Annotated[
        list[ExpansionStrategy] | None,
        typer.Option("--strategy"),
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.JSON,
) -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    _finish(
        run_expand(
            settings,
            seed,
            language=language,
            country=country,
            depth=depth,
            max_queries=max_queries,
            max_results=max_results,
            max_runtime_seconds=max_runtime_seconds,
            strategies=strategies,
            limit=limit,
        ),
        output_format,
    )


@app.command()
def trends(
    keywords: Annotated[list[str], typer.Argument()],
    language: Annotated[str | None, typer.Option("--language")] = None,
    country: Annotated[str | None, typer.Option("--country")] = None,
    timeframe: Annotated[str, typer.Option("--timeframe")] = "today 12-m",
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.JSON,
) -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    if keywords and keywords[0] == "compare":
        _finish(
            run_trends_compare(
                settings,
                keywords[1:],
                language=language,
                country=country,
                timeframe=timeframe,
            ),
            output_format,
        )
        return
    if len(keywords) != 1:
        raise typer.BadParameter(
            "Provide one keyword, or use 'trends compare <keyword> <keyword> ...'."
        )
    _finish(
        run_trends(
            settings,
            keywords[0],
            language=language,
            country=country,
            timeframe=timeframe,
        ),
        output_format,
    )


@ads_app.command("ideas")
def ads_ideas(
    keywords: Annotated[list[str] | None, typer.Argument()] = None,
    url: Annotated[str | None, typer.Option("--url")] = None,
    site: Annotated[str | None, typer.Option("--site")] = None,
    include_adult: Annotated[bool, typer.Option("--include-adult")] = False,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    language: Annotated[str | None, typer.Option("--language")] = None,
    country: Annotated[str | None, typer.Option("--country")] = None,
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.JSON,
) -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    _finish(
        run_ads_ideas(
            settings,
            keywords,
            url=url,
            site=site,
            include_adult=include_adult,
            limit=limit,
            language=language,
            country=country,
        ),
        output_format,
    )


@ads_app.command("historical")
def ads_historical(
    keywords: Annotated[list[str], typer.Argument()],
    language: Annotated[str | None, typer.Option("--language")] = None,
    country: Annotated[str | None, typer.Option("--country")] = None,
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.JSON,
) -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    _finish(
        run_ads_historical(
            settings,
            keywords,
            language=language,
            country=country,
        ),
        output_format,
    )


@gsc_app.command("properties")
def gsc_properties(
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.JSON,
) -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    _finish(run_gsc_properties(settings), output_format)


@gsc_app.command("queries")
def gsc_queries(
    site_url: str,
    days: Annotated[int, typer.Option("--days")] = 28,
    start_date: Annotated[str | None, typer.Option("--start-date")] = None,
    end_date: Annotated[str | None, typer.Option("--end-date")] = None,
    dimensions: Annotated[list[str] | None, typer.Option("--dimension")] = None,
    country: Annotated[str | None, typer.Option("--country")] = None,
    search_type: Annotated[str, typer.Option("--search-type")] = "web",
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.JSON,
) -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    _finish(
        run_gsc_queries(
            settings,
            site_url,
            days=days,
            start_date=start_date,
            end_date=end_date,
            dimensions=dimensions,
            country=country,
            search_type=search_type,
            limit=limit,
        ),
        output_format,
    )


@gsc_app.command("opportunities")
def gsc_opportunities(
    site_url: str,
    days: Annotated[int, typer.Option("--days")] = 28,
    country: Annotated[str | None, typer.Option("--country")] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.JSON,
) -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    _finish(
        run_gsc_opportunities(
            settings,
            site_url,
            days=days,
            country=country,
            limit=limit,
        ),
        output_format,
    )


@app.command()
def competitor(
    target: str,
    seed_keyword: Annotated[str | None, typer.Option("--seed-keyword")] = None,
    language: Annotated[str | None, typer.Option("--language")] = None,
    country: Annotated[str | None, typer.Option("--country")] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.JSON,
) -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    _finish(
        run_competitor(
            settings,
            target,
            seed_keyword=seed_keyword,
            language=language,
            country=country,
            limit=limit,
        ),
        output_format,
    )


@app.command()
def research(
    target: str,
    scenario: Annotated[str, typer.Option("--scenario")] = "auto",
    language: Annotated[str | None, typer.Option("--language")] = None,
    country: Annotated[str | None, typer.Option("--country")] = None,
    seed_keyword: Annotated[str | None, typer.Option("--seed-keyword")] = None,
    max_keywords: Annotated[int, typer.Option("--max-keywords")] = 2000,
    max_autocomplete_queries: Annotated[int, typer.Option("--max-autocomplete-queries")] = 500,
    max_ads_calls: Annotated[int, typer.Option("--max-ads-calls")] = 20,
    max_trends_calls: Annotated[int, typer.Option("--max-trends-calls")] = 3,
    max_runtime: Annotated[float, typer.Option("--max-runtime")] = 300.0,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.JSON,
) -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    _finish(
        cast(
            Envelope[object],
            run_research(
                settings,
                target,
                scenario=scenario,
                language=language,
                country=country,
                seed_keyword=seed_keyword,
                budget=Budget(
                    max_keywords=max_keywords,
                    max_autocomplete_queries=max_autocomplete_queries,
                    max_ads_calls=max_ads_calls,
                    max_trends_calls=max_trends_calls,
                    max_runtime_seconds=max_runtime,
                ),
                dry_run=dry_run,
                limit=limit,
            ),
        ),
        output_format,
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
