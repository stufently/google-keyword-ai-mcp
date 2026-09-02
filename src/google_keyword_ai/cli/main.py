import json
from enum import StrEnum
from typing import Annotated

import typer

from google_keyword_ai.config import load_settings
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.expansion import ExpansionStrategy
from google_keyword_ai.logging import configure_logging
from google_keyword_ai.usecases.doctor import run_config_show, run_doctor
from google_keyword_ai.usecases.expand import run_expand
from google_keyword_ai.usecases.suggest import run_suggest
from google_keyword_ai.usecases.trends import run_trends, run_trends_compare

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
config_app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
app.add_typer(config_app, name="config")


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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
