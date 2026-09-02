import json
from enum import StrEnum
from typing import Annotated

import typer

from google_keyword_ai.config import load_settings
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.logging import configure_logging
from google_keyword_ai.usecases.doctor import run_config_show, run_doctor

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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
