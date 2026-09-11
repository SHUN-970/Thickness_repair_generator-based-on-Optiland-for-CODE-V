"""Standalone CODE V / Optiland thickness-repair tool.

Implemented phases:
    - stable data contracts
    - prescription-style CODE V parser
    - spherical Optiland rebuild
    - EFL, field, and multi-wavelength real-ray validation
    - physical-lens layout labeling
    - legacy CT/ET and air-gap/TTL diagnosis
    - explicit selection and consecutive repair-block construction
    - preserved constant-power / PP-aware branching generator
    - generalized block-leading reconstruction and trailing clearance repair
    - isolated L1 H2 / trailing-gap boundary adapter
    - exact-token CODE V template patching
    - per-candidate physical-lens layouts and flat summary CSV
    - direct-run interactive and command-line workflow

The power-preserving algorithm is intentionally not reimplemented until its
verified notebook code is integrated in the designated generator phase.
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
import csv
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import hashlib
import math
import os
from pathlib import Path
import re
import shlex
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence


# ============================================================
# 1. Public constants
# ============================================================

DEFAULT_PRIMARY_WAVELENGTH_UM = 0.5876
RANDOM_SEED: int | None = None
DEFAULT_RAYTRACE_NUM_RAYS = 12
DEFAULT_RAYTRACE_DISTRIBUTION = "hexapolar"
DEFAULT_EFL_TOLERANCE_MM = 0.5
DEFAULT_CONSTRAINT_EPSILON_MM = 1.0e-9
DEFAULT_SPACING_EPSILON_MM = 1.0e-6
DEFAULT_CLI_LAYOUT_NUM_RAYS = 5


# ============================================================
# 2. Status types
# ============================================================

class WarningSeverity(str, Enum):
    """Severity of a parser or reconstruction warning."""

    WARNING = "WARNING"
    ERROR = "ERROR"


class ThicknessStatus(str, Enum):
    """Diagnosis result for one physical lens."""

    TOO_THIN = "TOO_THIN"
    OK = "OK"


class RayTraceStatus(str, Enum):
    """Final multi-field real-ray tracing state."""

    NOT_RUN = "NOT_RUN"
    PASS = "PASS"
    FAIL = "FAIL"


# ============================================================
# 3. CODE V source and prescription records
# ============================================================

@dataclass(frozen=True, slots=True)
class SourceValueRef:
    """Location of one patchable value in the original sequence text.

    ``line_index`` is zero-based. ``value_start`` and ``value_end`` are Python
    slice offsets in that line. Keeping the token span lets export replace only
    a radius or thickness value while preserving the rest of the line exactly.
    """

    line_index: int
    command: str
    value_start: int
    value_end: int
    original_token: str

    @property
    def line_number(self) -> int:
        """Return a one-based line number for messages shown to users."""

        return self.line_index + 1


@dataclass(frozen=True, slots=True)
class ParseWarning:
    """Structured warning produced while parsing or rebuilding a system."""

    code: str
    message: str
    severity: WarningSeverity = WarningSeverity.WARNING
    line_number: int | None = None


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One field point retained from the CODE V prescription."""

    x: float
    y: float
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class WavelengthSpec:
    """One wavelength retained from the CODE V prescription."""

    wavelength_um: float
    weight: float = 1.0
    is_primary: bool = False


@dataclass(frozen=True, slots=True)
class ApertureSpec:
    """System aperture definition without an Optiland dependency."""

    kind: str
    value: float


@dataclass(frozen=True, slots=True)
class SurfaceState:
    """Backend-neutral prescription data for one sequential surface."""

    codev_surface: int
    optiland_surface: int | None
    radius_mm: float | None
    thickness_mm: float | None
    glass: str | None
    surface_type: str
    is_stop: bool = False
    label: str | None = None
    clear_aperture_mm: float | None = None
    radius_source: SourceValueRef | None = None
    thickness_source: SourceValueRef | None = None


@dataclass(frozen=True, slots=True)
class LensDefinition:
    """Mapping for one physical lens, independent of surface display labels."""

    lens_id: str
    physical_index: int
    front_codev_surface: int
    rear_codev_surface: int
    front_optiland_surface: int
    rear_optiland_surface: int
    leading_gap_codev_surface: int | None
    trailing_gap_codev_surface: int | None
    glass: str | None = None


@dataclass(frozen=True, slots=True)
class OpticalState:
    """Canonical prescription state shared by parser, backend, and plugins."""

    surfaces: tuple[SurfaceState, ...]
    fields: tuple[FieldSpec, ...]
    wavelengths: tuple[WavelengthSpec, ...]
    aperture: ApertureSpec | None
    stop_codev_surface: int | None
    field_type: str = "REAL_IMAGE_HEIGHT"


@dataclass(frozen=True, slots=True)
class RayTraceCase:
    """Result for one normalized field and wavelength combination."""

    field_index: int
    hx: float
    hy: float
    wavelength_um: float
    passed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SystemValidation:
    """First-order and real-ray checks performed after Optiland rebuild."""

    efl_mm: float
    ttl_mm: float
    field_coordinates: tuple[tuple[float, float], ...]
    ray_trace_status: RayTraceStatus
    ray_trace_reason: str
    ray_trace_cases: tuple[RayTraceCase, ...]


@dataclass(slots=True)
class ParsedSequence:
    """Backend-neutral result of parsing one prescription-style sequence."""

    state: OpticalState
    raw_seq_text: str
    source_path: Path
    surface_map: dict[int, int]
    lens_map: dict[str, LensDefinition]
    warnings: list[ParseWarning] = field(default_factory=list)
    passthrough_commands: tuple[str, ...] = ()


@dataclass(slots=True)
class SystemBundle:
    """Loaded system plus its untouched CODE V source and all mappings."""

    optic: Any
    state: OpticalState
    raw_seq_text: str
    source_path: Path
    surface_map: dict[int, int]
    lens_map: dict[str, LensDefinition]
    warnings: list[ParseWarning] = field(default_factory=list)
    validation: SystemValidation | None = None
    layout_path: Path | None = None

    @property
    def fields(self) -> tuple[FieldSpec, ...]:
        return self.state.fields

    @property
    def wavelengths(self) -> tuple[WavelengthSpec, ...]:
        return self.state.wavelengths


# ============================================================
# 4. Prescription-style CODE V parser
# ============================================================

_SURFACE_PATTERN = re.compile(
    r"^(?P<indent>\s*)(?P<kind>SO|SI|S)\b"
    r"\s+(?P<radius>\S+)"
    r"(?:\s+(?P<thickness>\S+))?"
    r"(?:\s+(?P<glass>\S+))?",
    re.IGNORECASE,
)

_KNOWN_COMMANDS = {
    "RDM",
    "LEN",
    "TITLE",
    "FNO",
    "DIM",
    "WL",
    "REF",
    "WTW",
    "INI",
    "XRI",
    "YRI",
    "WTF",
    "VUX",
    "VUY",
    "VLX",
    "VLY",
    "DOR",
    "SO",
    "S",
    "SI",
    "SLB",
    "STO",
    "CCY",
    "THC",
    "UID",
    "GO",
    "CLS",
    "DER",
}

_ASPHERE_COMMANDS = {
    "ASP",
    "ASPH",
    "K",
    "QCON",
    "QCO",
    "QBFS",
    "QBF",
}


@dataclass(slots=True)
class _SurfaceBuilder:
    codev_surface: int
    radius_mm: float | None
    thickness_mm: float | None
    glass: str | None
    surface_type: str
    is_stop: bool
    label: str | None
    clear_aperture_mm: float | None
    radius_source: SourceValueRef | None
    thickness_source: SourceValueRef | None


def _split_inline_comment(line: str) -> tuple[str, str]:
    """Split a manual ``#`` comment without breaking quoted labels."""

    quote: str | None = None
    for index, character in enumerate(line):
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
        elif character == "#" and quote is None:
            return line[:index], line[index:]
    return line, ""


def _split_statements(code: str) -> list[tuple[str, int]]:
    """Split semicolon commands and retain each statement's column offset."""

    statements: list[tuple[str, int]] = []
    start = 0
    quote: str | None = None
    for index, character in enumerate(code):
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
        elif character == ";" and quote is None:
            raw_statement = code[start:index]
            leading_space = len(raw_statement) - len(raw_statement.lstrip())
            if raw_statement.strip():
                statements.append((raw_statement.strip(), start + leading_space))
            start = index + 1
    raw_statement = code[start:]
    leading_space = len(raw_statement) - len(raw_statement.lstrip())
    if raw_statement.strip():
        statements.append((raw_statement.strip(), start + leading_space))
    return statements


def _iter_logical_lines(lines: Sequence[str]) -> Iterable[tuple[int, str]]:
    """Join CODE V ``&`` continuations and retain the starting line index."""

    pending = ""
    start_index = 0
    for line_index, raw_line in enumerate(lines):
        code, _ = _split_inline_comment(raw_line)
        code = code.rstrip()
        if not pending:
            start_index = line_index
        logical_piece = code.lstrip() if pending else code
        continued = logical_piece.endswith("&")
        if continued:
            logical_piece = logical_piece[:-1].rstrip()
        pending = f"{pending} {logical_piece}".strip() if pending else logical_piece
        if not continued:
            if pending.strip():
                yield start_index, pending
            pending = ""
    if pending.strip():
        yield start_index, pending


def _parse_float(token: str, *, line_number: int, name: str) -> float:
    try:
        return float(token)
    except ValueError as exc:
        raise ValueError(
            f"Line {line_number}: invalid {name} value {token!r}."
        ) from exc


def _radius_from_codev(token: str, *, line_number: int) -> float:
    value = _parse_float(token, line_number=line_number, name="radius")
    return math.inf if value == 0.0 else value


def _thickness_from_codev(
    token: str | None,
    *,
    line_number: int,
    surface_kind: str,
) -> float | None:
    if token is None:
        return None
    value = _parse_float(token, line_number=line_number, name="thickness")
    if surface_kind == "SO" and abs(value) >= 1.0e12:
        return math.inf
    return value


def _numeric_tail(statement: str, *, line_number: int) -> list[float]:
    tokens = statement.split()[1:]
    return [
        _parse_float(token, line_number=line_number, name="numeric")
        for token in tokens
    ]


def _read_seq_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeError(f"Could not decode CODE V sequence: {path}")


def _build_lens_map(
    surfaces: Sequence[SurfaceState],
) -> dict[str, LensDefinition]:
    """Number powered glass components as physical lenses L1, L2, ... ."""

    surface_by_number = {surface.codev_surface: surface for surface in surfaces}
    lens_map: dict[str, LensDefinition] = {}
    physical_index = 0
    for front in surfaces:
        if not front.glass:
            continue
        rear = surface_by_number.get(front.codev_surface + 1)
        if rear is None:
            continue
        front_is_plane = front.radius_mm is None or math.isinf(front.radius_mm)
        rear_is_plane = rear.radius_mm is None or math.isinf(rear.radius_mm)
        if front_is_plane and rear_is_plane:
            continue

        physical_index += 1
        lens_id = f"L{physical_index}"
        lens_map[lens_id] = LensDefinition(
            lens_id=lens_id,
            physical_index=physical_index,
            front_codev_surface=front.codev_surface,
            rear_codev_surface=rear.codev_surface,
            front_optiland_surface=front.optiland_surface
            if front.optiland_surface is not None
            else front.codev_surface,
            rear_optiland_surface=rear.optiland_surface
            if rear.optiland_surface is not None
            else rear.codev_surface,
            leading_gap_codev_surface=(
                front.codev_surface - 1 if front.codev_surface > 0 else None
            ),
            trailing_gap_codev_surface=rear.codev_surface,
            glass=front.glass,
        )
    return lens_map


def parse_codev_seq(path: str | Path) -> ParsedSequence:
    """Parse the supported prescription-style subset without using Optiland.

    Unsupported commands are preserved in ``raw_seq_text`` and reported. The
    parser never guesses an asphere conversion.
    """

    source_path = Path(path).expanduser().resolve()
    raw_seq_text = _read_seq_text(source_path)
    physical_lines = raw_seq_text.splitlines()

    warnings: list[ParseWarning] = []
    passthrough_commands: set[str] = set()
    surface_builders: list[_SurfaceBuilder] = []
    current_surface: _SurfaceBuilder | None = None

    aperture: ApertureSpec | None = None
    dimension = "M"
    wavelengths_raw: list[float] = []
    wavelength_weights: list[float] = []
    reference_wavelength = 1
    x_fields: list[float] = []
    y_fields: list[float] = []
    field_weights: list[float] = []

    for line_index, logical_line in _iter_logical_lines(physical_lines):
        line_number = line_index + 1
        for statement, statement_offset in _split_statements(logical_line):
            command_match = re.match(r"^([A-Za-z][A-Za-z0-9_]*)\b", statement)
            if command_match is None:
                warnings.append(
                    ParseWarning(
                        code="UNPARSED_LINE",
                        message=f"Could not identify command: {statement}",
                        line_number=line_number,
                    )
                )
                continue

            command = command_match.group(1).upper()
            passthrough_commands.add(command)

            if command in _ASPHERE_COMMANDS or re.fullmatch(r"A\d+", command):
                if current_surface is not None:
                    current_surface.surface_type = "UNSUPPORTED_ASPHERE"
                warnings.append(
                    ParseWarning(
                        code="ASPHERE_UNSUPPORTED",
                        message=(
                            f"{command} is preserved but is not converted to an "
                            "Optiland surface."
                        ),
                        line_number=line_number,
                    )
                )
                continue

            if command not in _KNOWN_COMMANDS:
                warnings.append(
                    ParseWarning(
                        code="UNSUPPORTED_COMMAND",
                        message=f"Unsupported command {command} is preserved unchanged.",
                        line_number=line_number,
                    )
                )
                continue

            if command in {"SO", "S", "SI"}:
                surface_match = _SURFACE_PATTERN.match(statement)
                if surface_match is None:
                    raise ValueError(
                        f"Line {line_number}: malformed {command} surface record."
                    )
                if command == "SO":
                    codev_surface = 0
                elif surface_builders:
                    codev_surface = surface_builders[-1].codev_surface + 1
                else:
                    codev_surface = 1

                radius_token = surface_match.group("radius")
                thickness_token = surface_match.group("thickness")
                glass_token = surface_match.group("glass") if command == "S" else None
                radius_span = surface_match.span("radius")
                thickness_span = (
                    surface_match.span("thickness")
                    if thickness_token is not None
                    else None
                )
                current_surface = _SurfaceBuilder(
                    codev_surface=codev_surface,
                    radius_mm=_radius_from_codev(
                        radius_token,
                        line_number=line_number,
                    ),
                    thickness_mm=_thickness_from_codev(
                        thickness_token,
                        line_number=line_number,
                        surface_kind=command,
                    ),
                    glass=glass_token,
                    surface_type="SPHERICAL",
                    is_stop=False,
                    label=None,
                    clear_aperture_mm=None,
                    radius_source=SourceValueRef(
                        line_index=line_index,
                        command=command,
                        value_start=statement_offset + radius_span[0],
                        value_end=statement_offset + radius_span[1],
                        original_token=radius_token,
                    ),
                    thickness_source=(
                        SourceValueRef(
                            line_index=line_index,
                            command=command,
                            value_start=statement_offset + thickness_span[0],
                            value_end=statement_offset + thickness_span[1],
                            original_token=thickness_token,
                        )
                        if thickness_span is not None and thickness_token is not None
                        else None
                    ),
                )
                surface_builders.append(current_surface)
                continue

            if command == "SLB" and current_surface is not None:
                try:
                    tokens = shlex.split(statement, posix=True)
                except ValueError as exc:
                    raise ValueError(f"Line {line_number}: malformed SLB label.") from exc
                current_surface.label = tokens[1] if len(tokens) > 1 else ""
                continue

            if command == "STO" and current_surface is not None:
                current_surface.is_stop = True
                continue

            if command == "CCY" and current_surface is not None:
                values = _numeric_tail(statement, line_number=line_number)
                current_surface.clear_aperture_mm = values[0] if values else None
                continue

            if command == "FNO":
                values = _numeric_tail(statement, line_number=line_number)
                if values:
                    aperture = ApertureSpec(kind="FNO", value=values[0])
                continue

            if command == "DIM":
                tokens = statement.split()
                dimension = tokens[1].upper() if len(tokens) > 1 else ""
                if dimension != "M":
                    warnings.append(
                        ParseWarning(
                            code="UNSUPPORTED_DIMENSION",
                            message=f"DIM {dimension!r} is not supported by the mm workflow.",
                            line_number=line_number,
                        )
                    )
                continue

            if command == "WL":
                wavelengths_raw = _numeric_tail(statement, line_number=line_number)
                continue

            if command == "WTW":
                wavelength_weights = _numeric_tail(statement, line_number=line_number)
                continue

            if command == "REF":
                values = _numeric_tail(statement, line_number=line_number)
                if values:
                    reference_wavelength = int(values[0])
                continue

            if command == "XRI":
                x_fields = _numeric_tail(statement, line_number=line_number)
                continue

            if command == "YRI":
                y_fields = _numeric_tail(statement, line_number=line_number)
                continue

            if command == "WTF":
                field_weights = _numeric_tail(statement, line_number=line_number)
                continue

    if not surface_builders:
        raise ValueError("No sequential surfaces were found in the CODE V sequence.")

    surfaces = tuple(
        SurfaceState(
            codev_surface=builder.codev_surface,
            optiland_surface=builder.codev_surface,
            radius_mm=builder.radius_mm,
            thickness_mm=builder.thickness_mm,
            glass=builder.glass,
            surface_type=builder.surface_type,
            is_stop=builder.is_stop,
            label=builder.label,
            clear_aperture_mm=builder.clear_aperture_mm,
            radius_source=builder.radius_source,
            thickness_source=builder.thickness_source,
        )
        for builder in surface_builders
    )

    if not wavelengths_raw:
        warnings.append(
            ParseWarning(code="MISSING_WAVELENGTHS", message="No WL command was found.")
        )
    if wavelength_weights and len(wavelength_weights) != len(wavelengths_raw):
        warnings.append(
            ParseWarning(
                code="WAVELENGTH_WEIGHT_COUNT",
                message="WTW count does not match the WL count; missing weights use 1.0.",
            )
        )
    wavelengths = tuple(
        WavelengthSpec(
            wavelength_um=value / 1000.0 if abs(value) > 10.0 else value,
            weight=(
                wavelength_weights[index]
                if index < len(wavelength_weights)
                else 1.0
            ),
            is_primary=index + 1 == reference_wavelength,
        )
        for index, value in enumerate(wavelengths_raw)
    )

    field_count = max(len(x_fields), len(y_fields))
    if field_count == 0:
        warnings.append(ParseWarning(code="MISSING_FIELDS", message="No fields were found."))
    if x_fields and y_fields and len(x_fields) != len(y_fields):
        warnings.append(
            ParseWarning(
                code="FIELD_COUNT_MISMATCH",
                message="XRI and YRI field counts do not match.",
                severity=WarningSeverity.ERROR,
            )
        )
    if field_weights and len(field_weights) != field_count:
        warnings.append(
            ParseWarning(
                code="FIELD_WEIGHT_COUNT",
                message="WTF count does not match the field count; missing weights use 1.0.",
            )
        )
    fields = tuple(
        FieldSpec(
            x=x_fields[index] if index < len(x_fields) else 0.0,
            y=y_fields[index] if index < len(y_fields) else 0.0,
            weight=field_weights[index] if index < len(field_weights) else 1.0,
        )
        for index in range(field_count)
    )
    if fields and all(field.x == 0.0 and field.y == 0.0 for field in fields):
        warnings.append(
            ParseWarning(
                code="ON_AXIS_ONLY",
                message=(
                    "Only an on-axis field is defined; ray validation cannot "
                    "represent full-field feasibility."
                ),
            )
        )

    stop_surfaces = [surface.codev_surface for surface in surfaces if surface.is_stop]
    if len(stop_surfaces) > 1:
        warnings.append(
            ParseWarning(
                code="MULTIPLE_STOPS",
                message="More than one STO command was found.",
                severity=WarningSeverity.ERROR,
            )
        )
    stop_surface = stop_surfaces[0] if stop_surfaces else None
    surface_map = {surface.codev_surface: surface.codev_surface for surface in surfaces}
    lens_map = _build_lens_map(surfaces)
    state = OpticalState(
        surfaces=surfaces,
        fields=fields,
        wavelengths=wavelengths,
        aperture=aperture,
        stop_codev_surface=stop_surface,
        field_type="REAL_IMAGE_HEIGHT",
    )
    return ParsedSequence(
        state=state,
        raw_seq_text=raw_seq_text,
        source_path=source_path,
        surface_map=surface_map,
        lens_map=lens_map,
        warnings=warnings,
        passthrough_commands=tuple(sorted(passthrough_commands)),
    )


# ============================================================
# 5. Optiland rebuild and validation
# ============================================================

def _require_supported_rebuild(parsed: ParsedSequence) -> None:
    """Stop before Optiland rebuild when the parser found unsafe ambiguity."""

    blocking_codes = {
        "ASPHERE_UNSUPPORTED",
        "FIELD_COUNT_MISMATCH",
        "MULTIPLE_STOPS",
        "UNSUPPORTED_COMMAND",
        "UNSUPPORTED_DIMENSION",
    }
    blockers = [warning for warning in parsed.warnings if warning.code in blocking_codes]
    if blockers:
        details = "; ".join(
            f"{warning.code}"
            f" at line {warning.line_number}" if warning.line_number else warning.code
            for warning in blockers
        )
        raise NotImplementedError(
            "Optiland rebuild stopped because the sequence contains unsupported "
            f"or ambiguous content: {details}"
        )


def _normalize_codev_glass_name(name: str) -> str:
    """Insert the catalog hyphen commonly omitted by CODE V glass tokens."""

    prefixes = frozenset("NSPQEHLMKFGC")
    if (
        len(name) > 2
        and "-" not in name
        and name[0].upper() in prefixes
        and name[1].isalpha()
    ):
        return f"{name[0]}-{name[1:]}"
    return name


def _resolve_optiland_material(glass_token: str) -> Any:
    """Resolve a CODE V glass token without silently substituting air."""

    from optiland.materials import AbbeMaterial, Material

    token = glass_token.strip("'\"")
    if ":" in token:
        try:
            refractive_index, abbe_number = token.split(":", 1)
            return AbbeMaterial(
                n=float(refractive_index),
                abbe=float(abbe_number),
                # Match Optiland's CODE V reader for exact Fraunhofer wavelengths.
                model="buchdahl",
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid Nd:Vd glass token: {glass_token}") from exc

    if "_" in token:
        name, catalog = token.rsplit("_", 1)
        normalized_name = _normalize_codev_glass_name(name.upper())
        try:
            return Material(normalized_name, catalog=catalog.lower())
        except ValueError as exc:
            raise ValueError(
                f"Could not resolve CODE V glass {glass_token!r} in Optiland."
            ) from exc

    normalized_name = _normalize_codev_glass_name(token.upper())
    try:
        return Material(normalized_name)
    except ValueError as exc:
        raise ValueError(
            f"Could not resolve CODE V glass {glass_token!r} in Optiland."
        ) from exc


def rebuild_in_optiland(parsed: ParsedSequence) -> Any:
    """Rebuild a supported spherical sequence from the canonical state."""

    _require_supported_rebuild(parsed)
    try:
        from optiland import optic
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Optiland is not installed in the active Python environment."
        ) from exc

    optical_system = optic.Optic(name=parsed.source_path.stem)
    for surface in parsed.state.surfaces:
        if surface.surface_type != "SPHERICAL":
            raise NotImplementedError(
                f"S{surface.codev_surface} uses unsupported surface type "
                f"{surface.surface_type}."
            )

        surface_parameters: dict[str, Any] = {
            "index": surface.optiland_surface,
            "surface_type": "standard",
            "radius": surface.radius_mm if surface.radius_mm is not None else math.inf,
            "thickness": (
                surface.thickness_mm if surface.thickness_mm is not None else 0.0
            ),
            "is_stop": surface.is_stop,
            "comment": surface.label or "",
        }
        if surface.glass:
            surface_parameters["material"] = _resolve_optiland_material(surface.glass)
        optical_system.surfaces.add(**surface_parameters)

    aperture = parsed.state.aperture
    if aperture is None:
        raise ValueError("The sequence does not define a supported system aperture.")
    if aperture.kind != "FNO":
        raise NotImplementedError(
            f"Aperture type {aperture.kind!r} is not supported in this workflow."
        )
    optical_system.set_aperture(
        aperture_type="imageFNO",
        value=aperture.value,
    )

    if parsed.state.field_type != "REAL_IMAGE_HEIGHT":
        raise NotImplementedError(
            f"Field type {parsed.state.field_type!r} is not supported."
        )
    optical_system.fields.set_type(field_type="real_image_height")
    for field_spec in parsed.state.fields:
        optical_system.fields.add(
            x=field_spec.x,
            y=field_spec.y,
            weight=field_spec.weight,
        )

    if not parsed.state.wavelengths:
        raise ValueError("The sequence does not define any wavelengths.")
    for wavelength in parsed.state.wavelengths:
        optical_system.wavelengths.add(
            value=wavelength.wavelength_um,
            is_primary=wavelength.is_primary,
            weight=wavelength.weight,
        )

    optical_system.updater.update_paraxial()
    return optical_system


def _system_ttl_mm(
    optic_object: Any,
    lens_map: Mapping[str, LensDefinition],
    surface_map: Mapping[int, int],
) -> float:
    """Measure first-powered-surface to image-plane axial track."""

    import numpy as np

    if not lens_map:
        return math.nan
    first_lens_surface = min(
        lens.front_codev_surface for lens in lens_map.values()
    )
    image_surface = max(surface_map)
    return float(
        sum(
            float(
                np.asarray(
                    optic_object.surfaces.get_thickness(
                        surface_map[surface_number]
                    ),
                    dtype=np.float64,
                ).reshape(-1)[0]
            )
            for surface_number in range(first_lens_surface, image_surface)
        )
    )


def validate_rebuilt_system(
    optic_object: Any,
    lens_map: Mapping[str, LensDefinition],
    surface_map: Mapping[int, int],
    *,
    raytrace_num_rays: int = DEFAULT_RAYTRACE_NUM_RAYS,
    raytrace_distribution: str = DEFAULT_RAYTRACE_DISTRIBUTION,
) -> SystemValidation:
    """Measure EFL/TTL and perform all-field, all-wavelength real-ray tracing."""

    import numpy as np

    efl_mm = float(np.asarray(optic_object.paraxial.f2()).reshape(-1)[0])
    ttl_mm = _system_ttl_mm(optic_object, lens_map, surface_map)
    field_coordinates_array = np.asarray(
        optic_object.fields.get_field_coords(),
        dtype=np.float64,
    )
    field_coordinates = tuple(
        (float(field_coordinate[0]), float(field_coordinate[1]))
        for field_coordinate in field_coordinates_array
    )

    wavelength_values = tuple(
        float(wavelength.value) for wavelength in optic_object.wavelengths.wavelengths
    )
    cases: list[RayTraceCase] = []
    first_failure = "OK"
    ray_attribute_names = ("x", "y", "z", "L", "M", "N")

    for field_index, (hx, hy) in enumerate(field_coordinates):
        for wavelength_um in wavelength_values:
            passed = True
            reason = "OK"
            try:
                rays = optic_object.trace(
                    Hx=hx,
                    Hy=hy,
                    wavelength=wavelength_um,
                    num_rays=raytrace_num_rays,
                    distribution=raytrace_distribution,
                )
                for attribute_name in ray_attribute_names:
                    values = np.asarray(
                        getattr(rays, attribute_name),
                        dtype=np.float64,
                    )
                    if values.size == 0 or not np.all(np.isfinite(values)):
                        passed = False
                        reason = f"NONFINITE_{attribute_name}"
                        break
            except Exception as exc:
                passed = False
                reason = f"TRACE_EXCEPTION:{type(exc).__name__}"

            if not passed and first_failure == "OK":
                first_failure = (
                    f"field={field_index}, wavelength={wavelength_um:.7f}: {reason}"
                )
            cases.append(
                RayTraceCase(
                    field_index=field_index,
                    hx=hx,
                    hy=hy,
                    wavelength_um=wavelength_um,
                    passed=passed,
                    reason=reason,
                )
            )

    all_passed = bool(cases) and all(case.passed for case in cases)
    return SystemValidation(
        efl_mm=efl_mm,
        ttl_mm=ttl_mm,
        field_coordinates=field_coordinates,
        ray_trace_status=(
            RayTraceStatus.PASS if all_passed else RayTraceStatus.FAIL
        ),
        ray_trace_reason="OK" if all_passed else first_failure,
        ray_trace_cases=tuple(cases),
    )


def _save_labeled_optic_layout(
    optic_object: Any,
    lens_map: Mapping[str, LensDefinition],
    surface_map: Mapping[int, int],
    output_path: str | Path,
    *,
    title: str,
    label_styles: Mapping[str, tuple[str, str, str]] | None = None,
    label_stagger: bool = False,
    num_rays: int = 5,
) -> Path:
    """Draw one Optiland system and label its physical glass components."""

    import matplotlib.pyplot as plt
    import numpy as np

    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    figure, axes = optic_object.draw(
        fields="all",
        wavelengths="primary",
        num_rays=num_rays,
        show_apertures=True,
        hide_vignetted=False,
        figsize=(12, 5),
        title=title,
    )

    positions = np.asarray(optic_object.surfaces.positions, dtype=np.float64).reshape(-1)
    y_min, y_max = axes.get_ylim()
    label_y = y_max - 0.035 * (y_max - y_min)
    label_range = y_max - y_min
    for lens_id, lens in sorted(
        lens_map.items(),
        key=lambda item: item[1].physical_index,
    ):
        front_index = surface_map[lens.front_codev_surface]
        rear_index = surface_map[lens.rear_codev_surface]
        lens_center_z = 0.5 * (positions[front_index] + positions[rear_index])
        label_text, text_color, face_color = (
            label_styles.get(lens_id, (lens_id, "#172554", "white"))
            if label_styles is not None
            else (lens_id, "#172554", "white")
        )
        axes.text(
            lens_center_z,
            label_y
            - (
                ((lens.physical_index - 1) % 3) * 0.12 * label_range
                if label_stagger
                else 0.0
            ),
            label_text,
            ha="center",
            va="top",
            fontsize=8.5,
            fontweight="bold",
            color=text_color,
            bbox={
                "boxstyle": "round,pad=0.18",
                "facecolor": face_color,
                "edgecolor": "#94A3B8",
                "alpha": 0.9,
            },
            zorder=20,
        )

    figure.savefig(output, dpi=200, bbox_inches="tight")
    plt.close(figure)
    return output


def save_physical_lens_layout(
    system: SystemBundle,
    output_path: str | Path,
    *,
    num_rays: int = 5,
) -> Path:
    """Save a primary-wavelength layout labeled with physical lens IDs."""

    output = _save_labeled_optic_layout(
        system.optic,
        system.lens_map,
        system.surface_map,
        output_path,
        title=f"{system.source_path.stem} - Optiland rebuild",
        num_rays=num_rays,
    )
    system.layout_path = output
    return output


# ============================================================
# 6. Constraints and generation settings
# ============================================================

@dataclass(frozen=True, slots=True)
class Constraints:
    """The four hard feasibility limits entered by the user.

    The repair CT is the minimum allowed CT. EFL is measured automatically
    after legacy seed-gap normalization and is never entered by the user.
    """

    ct_min_mm: float
    et_min_mm: float
    center_gap_min_mm: float
    ttl_max_mm: float

    @property
    def target_repair_ct_mm(self) -> float:
        """Return the fixed repair CT used by the verified notebook logic."""

        return self.ct_min_mm

    def __post_init__(self) -> None:
        if self.ct_min_mm <= 0.0:
            raise ValueError("ct_min_mm must be positive.")
        if self.et_min_mm < 0.0:
            raise ValueError("et_min_mm must be non-negative.")
        if self.center_gap_min_mm < 0.0:
            raise ValueError("center_gap_min_mm must be non-negative.")
        if self.ttl_max_mm <= 0.0:
            raise ValueError("ttl_max_mm must be positive.")


@dataclass(frozen=True, slots=True)
class GenerationSettings:
    """Internal algorithm settings that are not requested from the user.

    ``random_seed=None`` lets NumPy initialize from system entropy, so normal
    CLI runs do not repeat the same candidate population. A fixed integer may
    still be supplied explicitly by calibration or regression code.
    """

    branch_factor: int = 3
    population_control_start_stage: int = 3
    min_population: int = 15
    target_population: int = 20
    max_population: int = 30
    refill_samples_per_parent: int = 1
    max_refill_rounds: int = 12
    target_valid_candidates: int = 10
    max_terminal_refill_rounds: int = 12
    random_seed: int | None = RANDOM_SEED
    raytrace_num_rays: int = DEFAULT_RAYTRACE_NUM_RAYS
    raytrace_distribution: str = DEFAULT_RAYTRACE_DISTRIBUTION
    efl_tolerance_mm: float = DEFAULT_EFL_TOLERANCE_MM
    constraint_epsilon_mm: float = DEFAULT_CONSTRAINT_EPSILON_MM
    spacing_epsilon_mm: float = DEFAULT_SPACING_EPSILON_MM

    def __post_init__(self) -> None:
        positive_names = (
            "branch_factor",
            "population_control_start_stage",
            "min_population",
            "target_population",
            "max_population",
            "refill_samples_per_parent",
            "max_refill_rounds",
            "target_valid_candidates",
            "max_terminal_refill_rounds",
            "raytrace_num_rays",
        )
        for name in positive_names:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive.")
        if not self.min_population <= self.target_population <= self.max_population:
            raise ValueError(
                "Population sizes must satisfy min <= target <= max."
            )
        if self.efl_tolerance_mm < 0.0:
            raise ValueError("efl_tolerance_mm must be non-negative.")
        if self.constraint_epsilon_mm < 0.0:
            raise ValueError("constraint_epsilon_mm must be non-negative.")
        if self.spacing_epsilon_mm <= 0.0:
            raise ValueError("spacing_epsilon_mm must be positive.")


# ============================================================
# 7. Diagnosis and explicit user selection
# ============================================================

@dataclass(frozen=True, slots=True)
class LensDiagnosis:
    """Thickness diagnosis for one physical lens."""

    lens_id: str
    front_codev_surface: int
    rear_codev_surface: int
    center_thickness_mm: float
    status: ThicknessStatus
    edge_thickness_mm: float | None = None
    edge_status: ThicknessStatus | None = None
    frozen_edge_radius_mm: float | None = None

    @property
    def is_compliant(self) -> bool:
        """Whether both CT and ET satisfy their respective constraints."""

        return (
            self.status is ThicknessStatus.OK
            and self.edge_status is ThicknessStatus.OK
        )

    @property
    def violation_reasons(self) -> tuple[str, ...]:
        """Return compact reasons suitable for a diagnosis table."""

        reasons: list[str] = []
        if self.status is ThicknessStatus.TOO_THIN:
            reasons.append("CT below minimum")
        if self.edge_status is ThicknessStatus.TOO_THIN:
            reasons.append("ET below minimum")
        elif self.edge_status is None:
            reasons.append("ET unavailable")
        return tuple(reasons)


@dataclass(frozen=True, slots=True)
class AirGapDiagnosis:
    """Legacy center- and edge-gap checks for one inter-component air space."""

    gap_codev_surface: int
    upstream_component: str
    downstream_component: str
    center_gap_mm: float
    edge_gap_mm: float | None
    frozen_edge_radius_mm: float | None
    center_gap_ok: bool
    edge_gap_ok: bool

    @property
    def is_compliant(self) -> bool:
        return self.center_gap_ok and self.edge_gap_ok


@dataclass(slots=True)
class DiagnosisReport:
    """Read-only diagnosis; it does not imply which lenses will be repaired."""

    system: SystemBundle
    constraints: Constraints
    lenses: list[LensDiagnosis]
    air_gaps: list[AirGapDiagnosis] = field(default_factory=list)
    total_thickness_mm: float | None = None
    total_thickness_ok: bool | None = None
    warnings: list[ParseWarning] = field(default_factory=list)

    @property
    def detected_thin_lenses(self) -> tuple[str, ...]:
        return tuple(
            row.lens_id
            for row in self.lenses
            if row.status is ThicknessStatus.TOO_THIN
        )

    @property
    def noncompliant_lenses(self) -> tuple[str, ...]:
        """Physical lenses selected when the user presses Enter at the prompt."""

        return tuple(row.lens_id for row in self.lenses if not row.is_compliant)

    @property
    def system_constraints_ok(self) -> bool:
        """Whether TTL and every legacy air-gap check pass."""

        return bool(self.total_thickness_ok) and all(
            gap.is_compliant for gap in self.air_gaps
        )


@dataclass(frozen=True, slots=True)
class RepairBlock:
    """One consecutive block derived from the user's selected lenses."""

    lens_ids: tuple[str, ...]
    leading_gap_codev_surface: int | None
    trailing_gap_codev_surface: int | None


@dataclass(slots=True)
class RepairRequest:
    """Explicit selection passed to the generator after diagnosis."""

    system: SystemBundle
    selected_lenses: tuple[str, ...]
    repair_blocks: tuple[RepairBlock, ...]
    constraints: Constraints
    settings: GenerationSettings


# ============================================================
# 8. Generation records
# ============================================================

@dataclass(frozen=True, slots=True)
class LensFirstOrderData:
    """Frozen first-order reference for one physical lens."""

    lens_id: str
    radius_1_mm: float
    radius_2_mm: float
    thickness_mm: float
    refractive_index: float
    power_per_mm: float
    h1_mm: float
    h2_mm: float


@dataclass(frozen=True, slots=True)
class GapAdjustment:
    """Minimal leading or trailing spacing change made by the generator."""

    gap_codev_surface: int
    old_gap_mm: float
    new_gap_mm: float
    reason: str


@dataclass(frozen=True, slots=True)
class GenerationBaseline:
    """Measured input and post-normalization references used by generation.

    ``generation_efl_mm`` is the automatic EFL target. It is measured only
    after reproducing the verified notebook's legacy seed-gap normalization.
    """

    input_efl_mm: float
    input_ttl_mm: float
    generation_efl_mm: float
    generation_ttl_mm: float
    lenses: tuple[LensFirstOrderData, ...]
    center_gaps_mm: Mapping[int, float]
    normalization_adjustments: tuple[GapAdjustment, ...] = ()
    frozen_lens_edge_radii_mm: Mapping[str, float] = field(default_factory=dict)
    frozen_air_edge_radii_mm: Mapping[int, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CandidateStep:
    """One sampled constant-power repair retained for audit and debugging."""

    stage_number: int
    lens_id: str
    h1_mm: float
    h2_mm: float
    radius_1_mm: float
    radius_2_mm: float
    preceding_gap_codev_surface: int | None
    preceding_gap_mm: float | None


@dataclass(frozen=True, slots=True)
class SurfacePatch:
    """Candidate values to patch into the original CODE V template."""

    radius_mm: float | None = None
    thickness_mm: float | None = None


@dataclass(frozen=True, slots=True)
class CandidateMetrics:
    """Feasibility metadata only; no image-quality score or rank is stored."""

    efl_mm: float
    ttl_mm: float
    center_thickness_mm: Mapping[str, float]
    edge_thickness_mm: Mapping[str, float]
    center_gaps_mm: Mapping[int, float]
    edge_gaps_mm: Mapping[int, float]

    @property
    def min_ct_mm(self) -> float:
        return min(self.center_thickness_mm.values())

    @property
    def min_et_mm(self) -> float:
        return min(self.edge_thickness_mm.values())

    @property
    def min_center_gap_mm(self) -> float:
        return min(self.center_gaps_mm.values())

    @property
    def min_edge_gap_mm(self) -> float:
        return min(self.edge_gaps_mm.values())


@dataclass(frozen=True, slots=True)
class RayTraceResult:
    """Outcome of final multi-field, multi-wavelength real-ray tracing."""

    status: RayTraceStatus = RayTraceStatus.NOT_RUN
    reason: str = "NOT_RUN"


@dataclass(slots=True)
class Candidate:
    """One structurally valid generated system and its export delta."""

    candidate_id: str
    optic: Any
    parent_id: str | None
    lineage: tuple[CandidateStep, ...]
    surface_patches: dict[int, SurfacePatch]
    gap_adjustments: tuple[GapAdjustment, ...]
    metrics: CandidateMetrics
    ray_trace: RayTraceResult


@dataclass(slots=True)
class GenerationResult:
    """All final ray-valid candidates; candidates are not ranked or sampled."""

    request: RepairRequest
    baseline: GenerationBaseline
    valid_candidates: list[Candidate]
    rejection_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[ParseWarning] = field(default_factory=list)
    stage_summaries: tuple["GenerationStageSummary", ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateExport:
    """Paths written for one ray-valid candidate."""

    candidate_id: str
    seq_path: Path
    layout_path: Path


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Complete, unranked export package for one generation run."""

    output_dir: Path
    candidates: tuple[CandidateExport, ...]
    summary_csv_path: Path
    input_layout_path: Path
    diagnosis_layout_path: Path
    diagnosis_text_path: Path


@dataclass(frozen=True, slots=True)
class GenerationStageSummary:
    """Population accounting for one deterministic repair stage."""

    stage_number: int
    lens_id: str
    parent_count: int
    initial_attempts: int
    initial_feasible: int
    refill_rounds: int
    refill_attempts: int
    population_before_pruning: int
    pruned_count: int
    carried_count: int
    rejection_counts: Mapping[str, int] = field(default_factory=dict)


# ============================================================
# 9. Selection and block helpers
# ============================================================

def build_repair_blocks(
    selected_lenses: Sequence[str],
    lens_map: Mapping[str, LensDefinition],
) -> tuple[RepairBlock, ...]:
    """Validate a selection and split it into consecutive physical blocks.

    Example: ``L3 L4 L7`` becomes ``[L3, L4]`` and ``[L7]``.
    """

    normalized = tuple(lens_id.strip().upper() for lens_id in selected_lenses)
    if not normalized:
        raise ValueError("At least one lens must be selected for repair.")
    if len(set(normalized)) != len(normalized):
        raise ValueError("The repair selection contains duplicate lens IDs.")

    unknown = [lens_id for lens_id in normalized if lens_id not in lens_map]
    if unknown:
        raise ValueError(f"Unknown lens IDs: {', '.join(unknown)}")

    ordered = sorted(normalized, key=lambda lens_id: lens_map[lens_id].physical_index)
    groups: list[list[str]] = []
    for lens_id in ordered:
        if not groups:
            groups.append([lens_id])
            continue

        previous = lens_map[groups[-1][-1]].physical_index
        current = lens_map[lens_id].physical_index
        if current == previous + 1:
            groups[-1].append(lens_id)
        else:
            groups.append([lens_id])

    blocks = []
    for group in groups:
        first_lens = lens_map[group[0]]
        last_lens = lens_map[group[-1]]
        blocks.append(
            RepairBlock(
                lens_ids=tuple(group),
                leading_gap_codev_surface=first_lens.leading_gap_codev_surface,
                trailing_gap_codev_surface=last_lens.trailing_gap_codev_surface,
            )
        )
    return tuple(blocks)


def create_repair_request(
    diagnosis: DiagnosisReport,
    selected_lenses: Sequence[str],
    settings: GenerationSettings | None = None,
) -> RepairRequest:
    """Create an explicit repair request without changing the optical system."""

    selected = tuple(lens_id.strip().upper() for lens_id in selected_lenses)
    blocks = build_repair_blocks(selected, diagnosis.system.lens_map)
    return RepairRequest(
        system=diagnosis.system,
        selected_lenses=tuple(lens_id for block in blocks for lens_id in block.lens_ids),
        repair_blocks=blocks,
        constraints=diagnosis.constraints,
        settings=settings or GenerationSettings(),
    )


_SELECTION_SPLIT_PATTERN = re.compile(r"[\s,;，；]+")
_SELECTION_RANGE_PATTERN = re.compile(r"L?(\d+)\s*-\s*L?(\d+)", re.IGNORECASE)
_CANCEL_SELECTIONS = {"NONE", "NO", "CANCEL", "取消", "不修"}
_ALL_SELECTIONS = {"ALL", "*", "全部"}


def parse_lens_selection(
    selection_text: str,
    diagnosis: DiagnosisReport,
) -> tuple[str, ...]:
    """Parse a human lens selection without changing the optical system.

    A blank selection means every lens whose CT or ET is noncompliant.  The
    user may enter IDs separated by spaces/commas (``L3 L4``), a range
    (``L3-L5``), ``ALL`` for every physical lens, or ``NONE`` to cancel.
    """

    cleaned = selection_text.strip()
    if not cleaned:
        return diagnosis.noncompliant_lenses

    keyword = cleaned.upper()
    if keyword in _CANCEL_SELECTIONS:
        return ()
    if keyword in _ALL_SELECTIONS:
        return tuple(
            lens_id
            for lens_id, _ in sorted(
                diagnosis.system.lens_map.items(),
                key=lambda item: item[1].physical_index,
            )
        )

    expanded: list[str] = []
    for token in _SELECTION_SPLIT_PATTERN.split(cleaned):
        if not token:
            continue
        range_match = _SELECTION_RANGE_PATTERN.fullmatch(token)
        if range_match:
            first = int(range_match.group(1))
            last = int(range_match.group(2))
            step = 1 if last >= first else -1
            expanded.extend(f"L{index}" for index in range(first, last + step, step))
            continue
        expanded.append(token.upper())

    if not expanded:
        return diagnosis.noncompliant_lenses
    if len(set(expanded)) != len(expanded):
        raise ValueError("The repair selection contains duplicate lens IDs.")

    unknown = [lens_id for lens_id in expanded if lens_id not in diagnosis.system.lens_map]
    if unknown:
        valid = ", ".join(diagnosis.system.lens_map)
        raise ValueError(
            f"Unknown lens IDs: {', '.join(unknown)}. Valid IDs are: {valid}."
        )
    return tuple(
        sorted(
            expanded,
            key=lambda lens_id: diagnosis.system.lens_map[lens_id].physical_index,
        )
    )


def select_lenses_for_repair(
    diagnosis: DiagnosisReport,
    selection_text: str | None = None,
    *,
    settings: GenerationSettings | None = None,
    input_fn: Callable[[str], str] = input,
) -> RepairRequest | None:
    """Obtain the user's selection and build the immutable repair request.

    ``selection_text=None`` displays an input prompt.  Passing ``""`` models
    pressing Enter and therefore selects every CT/ET-noncompliant lens.  An
    explicit ``NONE``/``取消`` returns ``None`` and is the only cancel path.
    """

    if selection_text is None:
        default_lenses = diagnosis.noncompliant_lenses
        default_text = ", ".join(default_lenses) if default_lenses else "none"
        selection_text = input_fn(
            "Select lenses (e.g. L3 L4 or L3-L5; "
            f"Enter = all noncompliant [{default_text}]; NONE = cancel): "
        )

    selected = parse_lens_selection(selection_text, diagnosis)
    if not selected:
        return None
    return create_repair_request(diagnosis, selected, settings=settings)


# ============================================================
# 10. Main workflow interfaces
# ============================================================

def load_codev_seq(path: str | Path) -> SystemBundle:
    """Parse a sequence, rebuild it in Optiland, and run baseline checks."""

    parsed = parse_codev_seq(path)
    optic_object = rebuild_in_optiland(parsed)
    validation = validate_rebuilt_system(
        optic_object,
        parsed.lens_map,
        parsed.surface_map,
    )
    warnings = list(parsed.warnings)
    if validation.ray_trace_status is RayTraceStatus.FAIL:
        warnings.append(
            ParseWarning(
                code="BASELINE_RAY_TRACE_FAILED",
                message=validation.ray_trace_reason,
                severity=WarningSeverity.ERROR,
            )
        )
    return SystemBundle(
        optic=optic_object,
        state=parsed.state,
        raw_seq_text=parsed.raw_seq_text,
        source_path=parsed.source_path,
        surface_map=parsed.surface_map,
        lens_map=parsed.lens_map,
        warnings=warnings,
        validation=validation,
    )


def _scalar_float(value: Any) -> float:
    """Convert a backend scalar to a plain finite Python float."""

    import numpy as np

    array = np.asarray(value, dtype=np.float64).reshape(-1)
    if array.size != 1:
        raise ValueError(f"Expected one scalar value, received shape {array.shape}.")
    result = float(array[0])
    if not math.isfinite(result):
        raise ValueError("Expected a finite scalar value.")
    return result


def _optic_surface(system: SystemBundle, codev_surface: int) -> Any:
    """Return an Optiland surface through the canonical CODE V mapping."""

    try:
        optiland_surface = system.surface_map[codev_surface]
    except KeyError as exc:
        raise KeyError(f"CODE V surface S{codev_surface} is not mapped.") from exc
    return system.optic.surfaces.surfaces[optiland_surface]


def _surface_edge_radii_mm(system: SystemBundle) -> dict[int, float]:
    """Freeze the verified legacy paraxial edge radius at every surface."""

    import numpy as np

    marginal_y, _ = system.optic.paraxial.marginal_ray()
    chief_y, _ = system.optic.paraxial.chief_ray()
    marginal = np.asarray(marginal_y, dtype=np.float64).reshape(-1)
    chief = np.asarray(chief_y, dtype=np.float64).reshape(-1)

    radii: dict[int, float] = {}
    for codev_surface, optiland_surface in system.surface_map.items():
        if optiland_surface >= marginal.size or optiland_surface >= chief.size:
            raise ValueError(
                f"Paraxial ray arrays do not contain mapped surface S{codev_surface}."
            )
        radii[codev_surface] = float(
            abs(marginal[optiland_surface]) + abs(chief[optiland_surface])
        )
    return radii


def _edge_thickness_mm(
    system: SystemBundle,
    lens_definition: LensDefinition,
    edge_radius_mm: float,
) -> float:
    """Evaluate ET using the unchanged notebook formula CT + sag2 - sag1."""

    front = _optic_surface(system, lens_definition.front_codev_surface)
    rear = _optic_surface(system, lens_definition.rear_codev_surface)
    center_thickness = _scalar_float(front.thickness)
    front_sag = _scalar_float(front.geometry.sag(edge_radius_mm, 0.0))
    rear_sag = _scalar_float(rear.geometry.sag(edge_radius_mm, 0.0))
    return float(center_thickness + rear_sag - front_sag)


def _air_edge_gap_mm(
    system: SystemBundle,
    gap_codev_surface: int,
    edge_radius_mm: float,
) -> float:
    """Evaluate the verified legacy air non-overlap quantity at one gap."""

    previous_surface = _optic_surface(system, gap_codev_surface)
    next_surface = _optic_surface(system, gap_codev_surface + 1)
    center_gap = _scalar_float(previous_surface.thickness)
    previous_sag = _scalar_float(previous_surface.geometry.sag(edge_radius_mm, 0.0))
    next_sag = _scalar_float(next_surface.geometry.sag(edge_radius_mm, 0.0))
    return float(center_gap + next_sag - previous_sag)


def _thickness_status(
    value_mm: float,
    minimum_mm: float,
    epsilon_mm: float = DEFAULT_CONSTRAINT_EPSILON_MM,
) -> ThicknessStatus:
    if value_mm < minimum_mm - epsilon_mm:
        return ThicknessStatus.TOO_THIN
    return ThicknessStatus.OK


def diagnose_thickness(
    system: SystemBundle,
    constraints: Constraints,
) -> DiagnosisReport:
    """Diagnose CT, ET, air gaps, and TTL without modifying the system.

    Lens ET and air-edge gaps use the original seed's frozen paraxial radii,
    exactly as in the verified notebook.  The default repair set contains only
    physical lenses whose CT or ET fails.  Air-gap and TTL failures remain
    system-level warnings because assigning them to one lens would be
    ambiguous.
    """

    surface_edge_radii = _surface_edge_radii_mm(system)
    ordered_lenses = sorted(
        system.lens_map.values(),
        key=lambda lens_definition: lens_definition.physical_index,
    )
    lens_edge_radii = {
        lens_definition.lens_id: max(
            surface_edge_radii[lens_definition.front_codev_surface],
            surface_edge_radii[lens_definition.rear_codev_surface],
        )
        for lens_definition in ordered_lenses
    }

    warnings = list(system.warnings)
    lens_rows: list[LensDiagnosis] = []
    for lens_definition in ordered_lenses:
        front = _optic_surface(system, lens_definition.front_codev_surface)
        center_thickness = _scalar_float(front.thickness)
        center_status = _thickness_status(
            center_thickness,
            constraints.ct_min_mm,
        )
        edge_radius = lens_edge_radii[lens_definition.lens_id]
        edge_thickness: float | None
        edge_status: ThicknessStatus | None
        try:
            edge_thickness = _edge_thickness_mm(
                system,
                lens_definition,
                edge_radius,
            )
            edge_status = (
                ThicknessStatus.TOO_THIN
                if edge_thickness
                < constraints.et_min_mm - DEFAULT_CONSTRAINT_EPSILON_MM
                else ThicknessStatus.OK
            )
        except (TypeError, ValueError, OverflowError) as exc:
            edge_thickness = None
            edge_status = None
            warnings.append(
                ParseWarning(
                    code="EDGE_THICKNESS_EVALUATION_FAILED",
                    message=f"{lens_definition.lens_id}: {exc}",
                    severity=WarningSeverity.ERROR,
                )
            )

        lens_rows.append(
            LensDiagnosis(
                lens_id=lens_definition.lens_id,
                front_codev_surface=lens_definition.front_codev_surface,
                rear_codev_surface=lens_definition.rear_codev_surface,
                center_thickness_mm=center_thickness,
                status=center_status,
                edge_thickness_mm=edge_thickness,
                edge_status=edge_status,
                frozen_edge_radius_mm=edge_radius,
            )
        )

    lens_by_front_surface = {
        lens_definition.front_codev_surface: lens_definition
        for lens_definition in ordered_lenses
    }
    air_gap_rows: list[AirGapDiagnosis] = []
    for upstream_lens in ordered_lenses:
        gap_surface = upstream_lens.trailing_gap_codev_surface
        if gap_surface is None or gap_surface + 1 not in system.surface_map:
            continue

        next_front_surface = gap_surface + 1
        downstream_lens = lens_by_front_surface.get(next_front_surface)
        if downstream_lens is not None:
            downstream_component = downstream_lens.lens_id
            downstream_radius = lens_edge_radii[downstream_lens.lens_id]
        else:
            component_surfaces = [next_front_surface]
            if next_front_surface + 1 in surface_edge_radii:
                component_surfaces.append(next_front_surface + 1)
            downstream_radius = max(
                surface_edge_radii[surface_number]
                for surface_number in component_surfaces
            )
            state_surface = next(
                (
                    surface
                    for surface in system.state.surfaces
                    if surface.codev_surface == next_front_surface
                ),
                None,
            )
            downstream_component = (
                state_surface.label.upper()
                if state_surface is not None and state_surface.label
                else f"S{next_front_surface} COMPONENT"
            )

        evaluation_radius = min(
            lens_edge_radii[upstream_lens.lens_id],
            downstream_radius,
        )
        center_gap = _scalar_float(_optic_surface(system, gap_surface).thickness)
        center_gap_ok = (
            center_gap
            >= constraints.center_gap_min_mm - DEFAULT_CONSTRAINT_EPSILON_MM
        )
        try:
            edge_gap = _air_edge_gap_mm(system, gap_surface, evaluation_radius)
            edge_gap_ok = edge_gap > 0.0
        except (TypeError, ValueError, OverflowError) as exc:
            edge_gap = None
            edge_gap_ok = False
            warnings.append(
                ParseWarning(
                    code="AIR_EDGE_GAP_EVALUATION_FAILED",
                    message=f"S{gap_surface}: {exc}",
                    severity=WarningSeverity.ERROR,
                )
            )

        if not center_gap_ok:
            warnings.append(
                ParseWarning(
                    code="CENTER_AIR_GAP_TOO_SMALL",
                    message=(
                        f"S{gap_surface} center gap {center_gap:.9f} mm is below "
                        f"{constraints.center_gap_min_mm:.9f} mm."
                    ),
                )
            )
        if not edge_gap_ok and edge_gap is not None:
            warnings.append(
                ParseWarning(
                    code="AIR_EDGE_OVERLAP",
                    message=(
                        f"S{gap_surface} legacy edge gap {edge_gap:.9f} mm "
                        "is not greater than zero."
                    ),
                )
            )

        air_gap_rows.append(
            AirGapDiagnosis(
                gap_codev_surface=gap_surface,
                upstream_component=upstream_lens.lens_id,
                downstream_component=downstream_component,
                center_gap_mm=center_gap,
                edge_gap_mm=edge_gap,
                frozen_edge_radius_mm=evaluation_radius,
                center_gap_ok=center_gap_ok,
                edge_gap_ok=edge_gap_ok,
            )
        )

    total_thickness = (
        system.validation.ttl_mm
        if system.validation is not None
        else _system_ttl_mm(system.optic, system.lens_map, system.surface_map)
    )
    total_thickness_ok = (
        total_thickness <= constraints.ttl_max_mm + DEFAULT_CONSTRAINT_EPSILON_MM
    )
    if not total_thickness_ok:
        warnings.append(
            ParseWarning(
                code="TOTAL_THICKNESS_TOO_LARGE",
                message=(
                    f"TTL {total_thickness:.9f} mm exceeds "
                    f"{constraints.ttl_max_mm:.9f} mm."
                ),
            )
        )

    return DiagnosisReport(
        system=system,
        constraints=constraints,
        lenses=lens_rows,
        air_gaps=air_gap_rows,
        total_thickness_mm=total_thickness,
        total_thickness_ok=total_thickness_ok,
        warnings=warnings,
    )


def format_diagnosis_report(report: DiagnosisReport) -> str:
    """Render a compact, stable text report for terminal or notebook use."""

    lines = [
        "PHYSICAL-LENS THICKNESS DIAGNOSIS",
        (
            "Limits: "
            f"CT>={report.constraints.ct_min_mm:.6f} mm, "
            f"ET>={report.constraints.et_min_mm:.6f} mm"
        ),
        "Lens  Surfaces       CT [mm]  CT status        ET [mm]  ET status   Overall",
        "----  --------  ------------  ----------  -------------  ----------  -------",
    ]
    for row in report.lenses:
        edge_value = (
            f"{row.edge_thickness_mm:.9f}"
            if row.edge_thickness_mm is not None
            else "N/A"
        )
        edge_status = row.edge_status.value if row.edge_status is not None else "N/A"
        lines.append(
            f"{row.lens_id:<4}  "
            f"S{row.front_codev_surface}-S{row.rear_codev_surface:<3}  "
            f"{row.center_thickness_mm:>12.9f}  "
            f"{row.status.value:<10}  "
            f"{edge_value:>13}  "
            f"{edge_status:<10}  "
            f"{'OK' if row.is_compliant else 'REPAIR'}"
        )

    default_selection = ", ".join(report.noncompliant_lenses) or "none"
    lines.extend(
        [
            "",
            f"Default repair selection (blank input): {default_selection}",
            "",
            "AIR-GAP / TOTAL-THICKNESS CHECKS",
            (
                "Gap   Components             Center [mm]  Center      "
                "Edge [mm]  Edge"
            ),
            (
                "----  ---------------------  -----------  ----------  "
                "-----------  ----------"
            ),
        ]
    )
    for gap in report.air_gaps:
        edge_value = f"{gap.edge_gap_mm:.9f}" if gap.edge_gap_mm is not None else "N/A"
        components = f"{gap.upstream_component}->{gap.downstream_component}"
        lines.append(
            f"S{gap.gap_codev_surface:<3}  {components:<21}  "
            f"{gap.center_gap_mm:>11.9f}  "
            f"{'PASS' if gap.center_gap_ok else 'FAIL':<10}  "
            f"{edge_value:>11}  "
            f"{'PASS' if gap.edge_gap_ok else 'FAIL':<10}"
        )

    ttl_value = (
        f"{report.total_thickness_mm:.9f} mm"
        if report.total_thickness_mm is not None
        else "N/A"
    )
    lines.append(
        f"TTL: {ttl_value} <= {report.constraints.ttl_max_mm:.9f} mm: "
        f"{'PASS' if report.total_thickness_ok else 'FAIL'}"
    )
    lines.append(
        "System constraints: "
        f"{'PASS' if report.system_constraints_ok else 'FAIL (see warnings)'}"
    )
    return "\n".join(lines)


def print_diagnosis_report(
    report: DiagnosisReport,
    output_fn: Callable[[str], Any] = print,
) -> None:
    """Print the diagnosis through an injectable output function."""

    output_fn(format_diagnosis_report(report))


@dataclass(frozen=True, slots=True)
class _RepairStage:
    lens_id: str
    lens: LensDefinition
    preceding_gap_codev_surface: int
    is_block_end: bool


@dataclass(slots=True)
class _SearchNode:
    candidate_id: str
    optic: Any
    parent_id: str | None
    lineage: tuple[CandidateStep, ...]
    metrics: CandidateMetrics


@dataclass(slots=True)
class _GenerationContext:
    request: RepairRequest
    ordered_lenses: tuple[LensDefinition, ...]
    stages: tuple[_RepairStage, ...]
    reference_lenses: Mapping[str, LensFirstOrderData]
    reference_gaps_mm: Mapping[int, float]
    frozen_lens_edge_radii_mm: Mapping[str, float]
    frozen_air_edge_radii_mm: Mapping[int, float]
    all_air_gap_surfaces: tuple[int, ...]
    preexisting_noncompliant_lenses: frozenset[str]
    search_seed: Any
    baseline: GenerationBaseline
    rng: Any
    serial: int = 0

    def next_candidate_id(self) -> str:
        self.serial += 1
        return f"C{self.serial:05d}"


def _clone_optic(optic_object: Any) -> Any:
    """Clone an Optiland system using the notebook's preferred round trip."""

    try:
        from optiland import optic

        return optic.Optic.from_dict(optic_object.to_dict())
    except Exception:
        return copy.deepcopy(optic_object)


def _surface_radius_mm(
    optic_object: Any,
    surface_map: Mapping[int, int],
    codev_surface: int,
) -> float:
    """Return a radius while allowing the valid plane value ``inf``."""

    import numpy as np

    value = optic_object.surfaces.surfaces[
        surface_map[codev_surface]
    ].geometry.radius
    array = np.asarray(value, dtype=np.float64).reshape(-1)
    if array.size != 1 or math.isnan(float(array[0])):
        raise ValueError(f"Invalid radius at S{codev_surface}.")
    return float(array[0])


def _surface_thickness_mm(
    optic_object: Any,
    surface_map: Mapping[int, int],
    codev_surface: int,
) -> float:
    return _scalar_float(
        optic_object.surfaces.get_thickness(surface_map[codev_surface])
    )


def _surface_thickness_allow_infinite(
    optic_object: Any,
    surface_map: Mapping[int, int],
    codev_surface: int,
) -> float:
    """Read object/image boundary thicknesses without rejecting infinity."""

    import numpy as np

    value = optic_object.surfaces.surfaces[
        surface_map[codev_surface]
    ].thickness
    array = np.asarray(value, dtype=np.float64).reshape(-1)
    if array.size != 1 or math.isnan(float(array[0])):
        raise ValueError(f"Invalid thickness at S{codev_surface}.")
    return float(array[0])


def _set_surface_thickness_mm(
    optic_object: Any,
    surface_map: Mapping[int, int],
    codev_surface: int,
    value_mm: float,
) -> None:
    optic_object.updater.set_thickness(value_mm, surface_map[codev_surface])


def _set_surface_radius_mm(
    optic_object: Any,
    surface_map: Mapping[int, int],
    codev_surface: int,
    value_mm: float,
) -> None:
    optic_object.updater.set_radius(value_mm, surface_map[codev_surface])


def _radius_to_curvature(radius_mm: float) -> float:
    if math.isinf(radius_mm):
        return 0.0
    return 1.0 / radius_mm


def _thick_lens_power(
    radius_1_mm: float,
    radius_2_mm: float,
    thickness_mm: float,
    refractive_index: float,
) -> float:
    c1 = _radius_to_curvature(radius_1_mm)
    c2 = _radius_to_curvature(radius_2_mm)
    phi_1 = (refractive_index - 1.0) * c1
    phi_2 = -(refractive_index - 1.0) * c2
    return float(
        phi_1
        + phi_2
        - thickness_mm / refractive_index * phi_1 * phi_2
    )


def _principal_plane_offsets(
    radius_1_mm: float,
    radius_2_mm: float,
    thickness_mm: float,
    refractive_index: float,
) -> tuple[float, float]:
    c1 = _radius_to_curvature(radius_1_mm)
    c2 = _radius_to_curvature(radius_2_mm)
    phi_1 = (refractive_index - 1.0) * c1
    phi_2 = -(refractive_index - 1.0) * c2
    power = (
        phi_1
        + phi_2
        - thickness_mm / refractive_index * phi_1 * phi_2
    )
    if abs(power) < 1.0e-14:
        raise ValueError("Near-zero thick-lens power.")
    h1 = thickness_mm * phi_2 / (refractive_index * power)
    h2 = -thickness_mm * phi_1 / (refractive_index * power)
    return float(h1), float(h2)


def _glass_index(
    optic_object: Any,
    surface_map: Mapping[int, int],
    front_codev_surface: int,
    wavelength_um: float,
) -> float:
    import numpy as np

    indices = np.asarray(
        optic_object.surfaces.n(wavelength_um),
        dtype=np.float64,
    ).reshape(-1)
    return float(indices[surface_map[front_codev_surface]])


def _lens_first_order_data(
    optic_object: Any,
    lens_definition: LensDefinition,
    surface_map: Mapping[int, int],
) -> LensFirstOrderData:
    radius_1 = _surface_radius_mm(
        optic_object,
        surface_map,
        lens_definition.front_codev_surface,
    )
    radius_2 = _surface_radius_mm(
        optic_object,
        surface_map,
        lens_definition.rear_codev_surface,
    )
    thickness = _surface_thickness_mm(
        optic_object,
        surface_map,
        lens_definition.front_codev_surface,
    )
    refractive_index = _glass_index(
        optic_object,
        surface_map,
        lens_definition.front_codev_surface,
        DEFAULT_PRIMARY_WAVELENGTH_UM,
    )
    power = _thick_lens_power(
        radius_1,
        radius_2,
        thickness,
        refractive_index,
    )
    h1, h2 = _principal_plane_offsets(
        radius_1,
        radius_2,
        thickness,
        refractive_index,
    )
    return LensFirstOrderData(
        lens_id=lens_definition.lens_id,
        radius_1_mm=radius_1,
        radius_2_mm=radius_2,
        thickness_mm=thickness,
        refractive_index=refractive_index,
        power_per_mm=power,
        h1_mm=h1,
        h2_mm=h2,
    )


def _constant_power_solution_from_h1(
    target_power: float,
    thickness_mm: float,
    refractive_index: float,
    h1_mm: float,
) -> tuple[float, float, float]:
    """Return ``(h2, R1, R2)`` using the verified Kingslake relation."""

    import numpy as np

    phi_2 = refractive_index * target_power * h1_mm / thickness_mm
    denominator = 1.0 - thickness_mm / refractive_index * phi_2
    if abs(denominator) < 1.0e-12:
        raise ValueError("Singular constant-power solution.")
    phi_1 = (target_power - phi_2) / denominator
    c1 = phi_1 / (refractive_index - 1.0)
    c2 = -phi_2 / (refractive_index - 1.0)
    radius_1 = math.inf if abs(c1) < 1.0e-14 else 1.0 / c1
    radius_2 = math.inf if abs(c2) < 1.0e-14 else 1.0 / c2
    if abs(target_power) < 1.0e-14:
        raise ValueError("Near-zero target power.")
    h2 = -thickness_mm * phi_1 / (refractive_index * target_power)
    values = np.asarray([h1_mm, h2, phi_1, phi_2], dtype=np.float64)
    if not np.all(np.isfinite(values)):
        raise ValueError("Non-finite constant-power solution.")
    return float(h2), float(radius_1), float(radius_2)


def _constant_power_solution_from_h2(
    target_power: float,
    thickness_mm: float,
    refractive_index: float,
    h2_mm: float,
) -> tuple[float, float, float]:
    """Mirror the Kingslake solve for the first-lens trailing boundary.

    L1 has no preceding powered lens, so its natural spacing actuator is the
    following air gap and its direct sampling coordinate is ``h2``.  Return
    ``(h1, R1, R2)`` without changing the established H1 path for L2 onward.
    """

    import numpy as np

    if abs(target_power) < 1.0e-14:
        raise ValueError("Near-zero target power.")
    phi_1 = -refractive_index * target_power * h2_mm / thickness_mm
    denominator = 1.0 - thickness_mm / refractive_index * phi_1
    if abs(denominator) < 1.0e-12:
        raise ValueError("Singular constant-power solution.")
    phi_2 = (target_power - phi_1) / denominator
    c1 = phi_1 / (refractive_index - 1.0)
    c2 = -phi_2 / (refractive_index - 1.0)
    radius_1 = math.inf if abs(c1) < 1.0e-14 else 1.0 / c1
    radius_2 = math.inf if abs(c2) < 1.0e-14 else 1.0 / c2
    h1 = thickness_mm * phi_2 / (refractive_index * target_power)
    values = np.asarray([h1, h2_mm, phi_1, phi_2], dtype=np.float64)
    if not np.all(np.isfinite(values)):
        raise ValueError("Non-finite constant-power solution.")
    return float(h1), float(radius_1), float(radius_2)


def _frozen_edge_maps(
    system: SystemBundle,
    ordered_lenses: Sequence[LensDefinition],
) -> tuple[dict[str, float], dict[int, float], tuple[int, ...]]:
    """Create legacy frozen lens and common air-edge evaluation radii."""

    surface_radii = _surface_edge_radii_mm(system)
    lens_radii = {
        lens.lens_id: max(
            surface_radii[lens.front_codev_surface],
            surface_radii[lens.rear_codev_surface],
        )
        for lens in ordered_lenses
    }
    lens_by_front = {lens.front_codev_surface: lens for lens in ordered_lenses}
    air_radii: dict[int, float] = {}
    gap_surfaces: list[int] = []
    for upstream_lens in ordered_lenses:
        gap_surface = upstream_lens.trailing_gap_codev_surface
        if gap_surface is None or gap_surface + 1 not in system.surface_map:
            continue
        gap_surfaces.append(gap_surface)
        downstream_lens = lens_by_front.get(gap_surface + 1)
        if downstream_lens is not None:
            downstream_radius = lens_radii[downstream_lens.lens_id]
        else:
            component_surfaces = [gap_surface + 1]
            if gap_surface + 2 in surface_radii:
                component_surfaces.append(gap_surface + 2)
            downstream_radius = max(
                surface_radii[surface_number]
                for surface_number in component_surfaces
            )
        air_radii[gap_surface] = min(
            lens_radii[upstream_lens.lens_id],
            downstream_radius,
        )
    return lens_radii, air_radii, tuple(gap_surfaces)


def _edge_thickness_for_optic(
    optic_object: Any,
    lens: LensDefinition,
    surface_map: Mapping[int, int],
    edge_radius_mm: float,
) -> float:
    front = optic_object.surfaces.surfaces[surface_map[lens.front_codev_surface]]
    rear = optic_object.surfaces.surfaces[surface_map[lens.rear_codev_surface]]
    center_thickness = _surface_thickness_mm(
        optic_object,
        surface_map,
        lens.front_codev_surface,
    )
    return float(
        center_thickness
        + _scalar_float(rear.geometry.sag(edge_radius_mm, 0.0))
        - _scalar_float(front.geometry.sag(edge_radius_mm, 0.0))
    )


def _air_edge_gap_for_optic(
    optic_object: Any,
    surface_map: Mapping[int, int],
    gap_surface: int,
    edge_radius_mm: float,
) -> float:
    previous_surface = optic_object.surfaces.surfaces[surface_map[gap_surface]]
    next_surface = optic_object.surfaces.surfaces[surface_map[gap_surface + 1]]
    return float(
        _surface_thickness_mm(optic_object, surface_map, gap_surface)
        + _scalar_float(next_surface.geometry.sag(edge_radius_mm, 0.0))
        - _scalar_float(previous_surface.geometry.sag(edge_radius_mm, 0.0))
    )


def _evaluate_candidate_metrics(
    context: _GenerationContext,
    optic_object: Any,
) -> CandidateMetrics:
    import numpy as np

    system = context.request.system
    try:
        efl_mm = _scalar_float(optic_object.paraxial.f2())
    except Exception:
        efl_mm = math.nan
    ttl_mm = _system_ttl_mm(optic_object, system.lens_map, system.surface_map)
    center_thickness: dict[str, float] = {}
    edge_thickness: dict[str, float] = {}
    for lens in context.ordered_lenses:
        center_thickness[lens.lens_id] = _surface_thickness_mm(
            optic_object,
            system.surface_map,
            lens.front_codev_surface,
        )
        try:
            edge_thickness[lens.lens_id] = _edge_thickness_for_optic(
                optic_object,
                lens,
                system.surface_map,
                context.frozen_lens_edge_radii_mm[lens.lens_id],
            )
        except Exception:
            edge_thickness[lens.lens_id] = math.nan

    center_gaps = {
        gap_surface: _surface_thickness_mm(
            optic_object,
            system.surface_map,
            gap_surface,
        )
        for gap_surface in context.all_air_gap_surfaces
    }
    edge_gaps: dict[int, float] = {}
    for gap_surface in context.all_air_gap_surfaces:
        try:
            edge_gaps[gap_surface] = _air_edge_gap_for_optic(
                optic_object,
                system.surface_map,
                gap_surface,
                context.frozen_air_edge_radii_mm[gap_surface],
            )
        except Exception:
            edge_gaps[gap_surface] = math.nan

    # Keep dictionaries as plain floats; this also rejects backend scalar leaks.
    assert all(isinstance(value, float) for value in center_thickness.values())
    assert all(isinstance(value, float) for value in edge_thickness.values())
    assert np.asarray(tuple(center_gaps.values()), dtype=np.float64).ndim == 1
    return CandidateMetrics(
        efl_mm=float(efl_mm),
        ttl_mm=float(ttl_mm),
        center_thickness_mm=center_thickness,
        edge_thickness_mm=edge_thickness,
        center_gaps_mm=center_gaps,
        edge_gaps_mm=edge_gaps,
    )


def _build_generation_context(request: RepairRequest) -> _GenerationContext:
    import numpy as np

    system = request.system
    settings = request.settings
    selected = request.selected_lenses
    if not selected:
        raise ValueError("At least one lens must be selected for generation.")
    ordered_lenses = tuple(
        lens
        for _, lens in sorted(
            system.lens_map.items(),
            key=lambda item: item[1].physical_index,
        )
    )
    block_end_ids = {block.lens_ids[-1] for block in request.repair_blocks}
    stage_rows: list[_RepairStage] = []
    for lens_id in selected:
        lens = system.lens_map[lens_id]
        # L1 preserves its boundary to L2 through the trailing S4 gap.  Every
        # later lens keeps the established preceding-gap H1 reconstruction.
        reconstruction_gap = (
            lens.trailing_gap_codev_surface
            if lens.physical_index == 1
            else lens.leading_gap_codev_surface
        )
        stage_rows.append(
            _RepairStage(
                lens_id=lens_id,
                lens=lens,
                preceding_gap_codev_surface=(
                    reconstruction_gap if reconstruction_gap is not None else -1
                ),
                is_block_end=lens_id in block_end_ids,
            )
        )
    stages = tuple(stage_rows)
    if any(stage.preceding_gap_codev_surface not in system.surface_map for stage in stages):
        raise ValueError("A selected lens does not have a mapped preceding gap.")

    reference_lenses_tuple = tuple(
        _lens_first_order_data(system.optic, lens, system.surface_map)
        for lens in ordered_lenses
    )
    reference_lenses = {
        lens_data.lens_id: lens_data for lens_data in reference_lenses_tuple
    }
    lens_edge_radii, air_edge_radii, all_gap_surfaces = _frozen_edge_maps(
        system,
        ordered_lenses,
    )
    reference_gaps = {
        gap_surface: _surface_thickness_mm(
            system.optic,
            system.surface_map,
            gap_surface,
        )
        for gap_surface in all_gap_surfaces
    }

    search_seed = _clone_optic(system.optic)
    dynamic_preceding_gaps = {
        stage.preceding_gap_codev_surface for stage in stages
    }
    delayed_edge_gaps = {
        block.trailing_gap_codev_surface
        for block in request.repair_blocks
        if block.trailing_gap_codev_surface in air_edge_radii
    }
    normalization_adjustments: list[GapAdjustment] = []
    for gap_surface in all_gap_surfaces:
        if gap_surface in dynamic_preceding_gaps:
            continue
        old_gap = _surface_thickness_mm(
            search_seed,
            system.surface_map,
            gap_surface,
        )
        required_gap = max(
            old_gap,
            request.constraints.center_gap_min_mm + settings.spacing_epsilon_mm,
        )
        reason = "TRAILING_CENTER_PRENORMALIZATION"
        if gap_surface not in delayed_edge_gaps:
            edge_radius = air_edge_radii[gap_surface]
            previous_surface = search_seed.surfaces.surfaces[
                system.surface_map[gap_surface]
            ]
            next_surface = search_seed.surfaces.surfaces[
                system.surface_map[gap_surface + 1]
            ]
            previous_sag = _scalar_float(
                previous_surface.geometry.sag(edge_radius, 0.0)
            )
            next_sag = _scalar_float(next_surface.geometry.sag(edge_radius, 0.0))
            required_by_edge = previous_sag - next_sag
            required_gap = max(
                required_gap,
                required_by_edge + settings.spacing_epsilon_mm,
            )
            reason = "STATIC_CENTER_AND_EDGE_NORMALIZATION"
        if required_gap > old_gap:
            _set_surface_thickness_mm(
                search_seed,
                system.surface_map,
                gap_surface,
                required_gap,
            )
            normalization_adjustments.append(
                GapAdjustment(
                    gap_codev_surface=gap_surface,
                    old_gap_mm=old_gap,
                    new_gap_mm=required_gap,
                    reason=reason,
                )
            )
    search_seed.updater.update_paraxial()

    input_efl = (
        system.validation.efl_mm
        if system.validation is not None
        else _scalar_float(system.optic.paraxial.f2())
    )
    input_ttl = (
        system.validation.ttl_mm
        if system.validation is not None
        else _system_ttl_mm(system.optic, system.lens_map, system.surface_map)
    )
    generation_efl = _scalar_float(search_seed.paraxial.f2())
    generation_ttl = _system_ttl_mm(
        search_seed,
        system.lens_map,
        system.surface_map,
    )
    baseline = GenerationBaseline(
        input_efl_mm=input_efl,
        input_ttl_mm=input_ttl,
        generation_efl_mm=generation_efl,
        generation_ttl_mm=generation_ttl,
        lenses=reference_lenses_tuple,
        center_gaps_mm=reference_gaps,
        normalization_adjustments=tuple(normalization_adjustments),
        frozen_lens_edge_radii_mm=lens_edge_radii,
        frozen_air_edge_radii_mm=air_edge_radii,
    )
    preexisting_noncompliant_lenses = frozenset(
        lens.lens_id
        for lens in ordered_lenses
        if (
            reference_lenses[lens.lens_id].thickness_mm
            < request.constraints.ct_min_mm - settings.constraint_epsilon_mm
            or _edge_thickness_for_optic(
                system.optic,
                lens,
                system.surface_map,
                lens_edge_radii[lens.lens_id],
            )
            < request.constraints.et_min_mm - settings.constraint_epsilon_mm
        )
    )
    context = _GenerationContext(
        request=request,
        ordered_lenses=ordered_lenses,
        stages=stages,
        reference_lenses=reference_lenses,
        reference_gaps_mm=reference_gaps,
        frozen_lens_edge_radii_mm=lens_edge_radii,
        frozen_air_edge_radii_mm=air_edge_radii,
        all_air_gap_surfaces=all_gap_surfaces,
        preexisting_noncompliant_lenses=preexisting_noncompliant_lenses,
        search_seed=search_seed,
        baseline=baseline,
        rng=np.random.default_rng(settings.random_seed),
    )
    return context


def _structural_feasibility_check(
    context: _GenerationContext,
    optic_object: Any,
    repaired_stage_count: int,
) -> tuple[bool, str, CandidateMetrics]:
    """Apply the old progressive hard checks to a generalized selection."""

    constraints = context.request.constraints
    settings = context.request.settings
    metrics = _evaluate_candidate_metrics(context, optic_object)

    if (
        not math.isfinite(metrics.efl_mm)
        or abs(metrics.efl_mm - context.baseline.generation_efl_mm)
        > settings.efl_tolerance_mm
    ):
        return False, "EFL", metrics
    if (
        not math.isfinite(metrics.ttl_mm)
        or metrics.ttl_mm
        > constraints.ttl_max_mm + settings.constraint_epsilon_mm
    ):
        return False, "TTL", metrics

    selected = context.request.selected_lenses
    selected_set = set(selected)
    finalized_lenses = [
        lens.lens_id
        for lens in context.ordered_lenses
        if (
            lens.lens_id not in selected_set
            and lens.lens_id not in context.preexisting_noncompliant_lenses
        )
    ]
    finalized_lenses.extend(selected[:repaired_stage_count])
    for lens_id in finalized_lenses:
        center_thickness = metrics.center_thickness_mm[lens_id]
        if (
            not math.isfinite(center_thickness)
            or center_thickness
            < constraints.ct_min_mm - settings.constraint_epsilon_mm
        ):
            return False, f"CT_{lens_id}", metrics
        edge_thickness = metrics.edge_thickness_mm[lens_id]
        if (
            not math.isfinite(edge_thickness)
            or edge_thickness
            < constraints.et_min_mm - settings.constraint_epsilon_mm
        ):
            return False, f"ET_{lens_id}", metrics

    unprocessed = set(selected[repaired_stage_count:])
    unprocessed_preceding_gaps = {
        context.request.system.lens_map[lens_id].leading_gap_codev_surface
        for lens_id in unprocessed
    }
    for gap_surface in context.all_air_gap_surfaces:
        if gap_surface in unprocessed_preceding_gaps:
            continue
        gap_value = metrics.center_gaps_mm[gap_surface]
        if (
            not math.isfinite(gap_value)
            or gap_value
            < constraints.center_gap_min_mm - settings.constraint_epsilon_mm
        ):
            return False, f"AIR_GAP_CENTER_S{gap_surface}", metrics

    lens_by_front = {
        lens.front_codev_surface: lens.lens_id for lens in context.ordered_lenses
    }
    lens_by_trailing_gap = {
        lens.trailing_gap_codev_surface: lens.lens_id
        for lens in context.ordered_lenses
    }
    for gap_surface in context.all_air_gap_surfaces:
        upstream_lens = lens_by_trailing_gap.get(gap_surface)
        downstream_lens = lens_by_front.get(gap_surface + 1)
        if upstream_lens in unprocessed or downstream_lens in unprocessed:
            continue
        edge_gap = metrics.edge_gaps_mm[gap_surface]
        if (
            not math.isfinite(edge_gap)
            or edge_gap <= settings.constraint_epsilon_mm
        ):
            return False, f"AIR_GAP_EDGE_S{gap_surface}", metrics

    return True, "OK", metrics


def _feasible_h1_interval(
    context: _GenerationContext,
    parent_optic: Any,
    stage: _RepairStage,
) -> tuple[float, float, float, float]:
    lens = stage.lens
    previous_index = lens.physical_index - 1
    previous_lens = next(
        candidate
        for candidate in context.ordered_lenses
        if candidate.physical_index == previous_index
    )
    current_reference = context.reference_lenses[lens.lens_id]
    previous_reference = context.reference_lenses[previous_lens.lens_id]
    previous_current = _lens_first_order_data(
        parent_optic,
        previous_lens,
        context.request.system.surface_map,
    )
    previous_h2_shift = previous_current.h2_mm - previous_reference.h2_mm
    reference_gap = context.reference_gaps_mm[stage.preceding_gap_codev_surface]
    parent_ttl = _system_ttl_mm(
        parent_optic,
        context.request.system.lens_map,
        context.request.system.surface_map,
    )
    current_ct = _surface_thickness_mm(
        parent_optic,
        context.request.system.surface_map,
        lens.front_codev_surface,
    )
    ct_increment = context.request.constraints.target_repair_ct_mm - current_ct
    h1_lower = (
        parent_ttl
        + ct_increment
        + current_reference.h1_mm
        + previous_h2_shift
        - context.request.constraints.ttl_max_mm
    )
    h1_upper = (
        reference_gap
        + current_reference.h1_mm
        + previous_h2_shift
        - context.request.constraints.center_gap_min_mm
    )
    return (
        float(h1_lower),
        float(h1_upper),
        float(previous_h2_shift),
        float(reference_gap),
    )


def _feasible_h2_interval_for_l1(
    context: _GenerationContext,
    parent_optic: Any,
    stage: _RepairStage,
) -> tuple[float, float, float]:
    """Direct center-gap/TTL interval for L1's trailing-boundary H2."""

    reference = context.reference_lenses[stage.lens_id]
    reference_gap = context.reference_gaps_mm[stage.preceding_gap_codev_surface]
    current_gap = _surface_thickness_mm(
        parent_optic,
        context.request.system.surface_map,
        stage.preceding_gap_codev_surface,
    )
    current_ct = _surface_thickness_mm(
        parent_optic,
        context.request.system.surface_map,
        stage.lens.front_codev_surface,
    )
    ct_increment = context.request.constraints.target_repair_ct_mm - current_ct
    parent_ttl = _system_ttl_mm(
        parent_optic,
        context.request.system.lens_map,
        context.request.system.surface_map,
    )
    h2_lower = (
        context.request.constraints.center_gap_min_mm
        - reference_gap
        + reference.h2_mm
    )
    h2_upper = (
        context.request.constraints.ttl_max_mm
        - parent_ttl
        - ct_increment
        - reference_gap
        + reference.h2_mm
        + current_gap
    )
    return float(h2_lower), float(h2_upper), float(reference_gap)


def _repair_trailing_gap(
    context: _GenerationContext,
    optic_object: Any,
    stage: _RepairStage,
) -> None:
    """Apply the generalized old S16 center/edge minimum movement."""

    gap_surface = stage.lens.trailing_gap_codev_surface
    if gap_surface not in context.frozen_air_edge_radii_mm:
        return
    system = context.request.system
    settings = context.request.settings
    edge_radius = context.frozen_air_edge_radii_mm[gap_surface]
    previous_surface = optic_object.surfaces.surfaces[
        system.surface_map[gap_surface]
    ]
    next_surface = optic_object.surfaces.surfaces[
        system.surface_map[gap_surface + 1]
    ]
    previous_sag = _scalar_float(previous_surface.geometry.sag(edge_radius, 0.0))
    next_sag = _scalar_float(next_surface.geometry.sag(edge_radius, 0.0))
    current_gap = _surface_thickness_mm(
        optic_object,
        system.surface_map,
        gap_surface,
    )
    required_by_edge = previous_sag - next_sag + settings.spacing_epsilon_mm
    repaired_gap = max(
        current_gap,
        context.request.constraints.center_gap_min_mm + settings.spacing_epsilon_mm,
        required_by_edge,
    )
    _set_surface_thickness_mm(
        optic_object,
        system.surface_map,
        gap_surface,
        repaired_gap,
    )


def _generate_one_child(
    context: _GenerationContext,
    parent: _SearchNode,
    stage: _RepairStage,
    repaired_stage_count: int,
) -> tuple[_SearchNode | None, str]:
    import numpy as np

    settings = context.request.settings
    reference = context.reference_lenses[stage.lens_id]
    if stage.lens.physical_index == 1:
        try:
            h2_lower, h2_upper, reference_gap = _feasible_h2_interval_for_l1(
                context,
                parent.optic,
                stage,
            )
        except Exception:
            return None, "H2_INTERVAL_ERROR"
        lower = h2_lower + settings.spacing_epsilon_mm
        upper = h2_upper - settings.spacing_epsilon_mm
        if not np.isfinite(lower) or not np.isfinite(upper) or upper <= lower:
            return None, "NO_H2_INTERVAL"
        h2 = float(context.rng.uniform(lower, upper))
        try:
            h1_sample, radius_1, radius_2 = _constant_power_solution_from_h2(
                target_power=reference.power_per_mm,
                thickness_mm=context.request.constraints.target_repair_ct_mm,
                refractive_index=reference.refractive_index,
                h2_mm=h2,
            )
        except Exception:
            return None, "CONSTANT_POWER_SOLVE"
        new_gap = reference_gap + h2 - reference.h2_mm
    else:
        try:
            h1_lower, h1_upper, previous_h2_shift, reference_gap = (
                _feasible_h1_interval(context, parent.optic, stage)
            )
        except Exception:
            return None, "H1_INTERVAL_ERROR"
        lower = h1_lower + settings.spacing_epsilon_mm
        upper = h1_upper - settings.spacing_epsilon_mm
        if not np.isfinite(lower) or not np.isfinite(upper) or upper <= lower:
            return None, "NO_H1_INTERVAL"
        h1_sample = float(context.rng.uniform(lower, upper))
        try:
            h2, radius_1, radius_2 = _constant_power_solution_from_h1(
                target_power=reference.power_per_mm,
                thickness_mm=context.request.constraints.target_repair_ct_mm,
                refractive_index=reference.refractive_index,
                h1_mm=h1_sample,
            )
        except Exception:
            return None, "CONSTANT_POWER_SOLVE"
        new_gap = (
            reference_gap
            + reference.h1_mm
            - h1_sample
            + previous_h2_shift
        )
    try:
        child_optic = _clone_optic(parent.optic)
        surface_map = context.request.system.surface_map
        _set_surface_thickness_mm(
            child_optic,
            surface_map,
            stage.lens.front_codev_surface,
            context.request.constraints.target_repair_ct_mm,
        )
        _set_surface_radius_mm(
            child_optic,
            surface_map,
            stage.lens.front_codev_surface,
            radius_1,
        )
        _set_surface_radius_mm(
            child_optic,
            surface_map,
            stage.lens.rear_codev_surface,
            radius_2,
        )
        _set_surface_thickness_mm(
            child_optic,
            surface_map,
            stage.preceding_gap_codev_surface,
            new_gap,
        )
        if stage.is_block_end:
            _repair_trailing_gap(context, child_optic, stage)
        child_optic.updater.update_paraxial()
    except Exception:
        return None, "OPTILAND_UPDATE"

    feasible, reason, metrics = _structural_feasibility_check(
        context,
        child_optic,
        repaired_stage_count,
    )
    if not feasible:
        return None, reason
    step = CandidateStep(
        stage_number=repaired_stage_count,
        lens_id=stage.lens_id,
        h1_mm=h1_sample,
        h2_mm=h2,
        radius_1_mm=radius_1,
        radius_2_mm=radius_2,
        preceding_gap_codev_surface=stage.preceding_gap_codev_surface,
        preceding_gap_mm=new_gap,
    )
    child = _SearchNode(
        candidate_id=context.next_candidate_id(),
        optic=child_optic,
        parent_id=parent.candidate_id,
        lineage=parent.lineage + (step,),
        metrics=metrics,
    )
    return child, "OK"


def _generate_children(
    context: _GenerationContext,
    parents: Sequence[_SearchNode],
    stage: _RepairStage,
    repaired_stage_count: int,
    samples_per_parent: int,
) -> tuple[list[_SearchNode], int, Counter[str]]:
    children: list[_SearchNode] = []
    rejection_counts: Counter[str] = Counter()
    attempts = 0
    for parent in parents:
        for _ in range(samples_per_parent):
            attempts += 1
            child, reason = _generate_one_child(
                context,
                parent,
                stage,
                repaired_stage_count,
            )
            if child is None:
                rejection_counts[reason] += 1
            else:
                children.append(child)
    return children, attempts, rejection_counts


def _values_differ(original: float, candidate: float, epsilon: float = 1.0e-12) -> bool:
    if math.isinf(original) or math.isinf(candidate):
        return not (
            math.isinf(original)
            and math.isinf(candidate)
            and math.copysign(1.0, original) == math.copysign(1.0, candidate)
        )
    return abs(original - candidate) > epsilon


def _candidate_surface_patches(
    context: _GenerationContext,
    optic_object: Any,
) -> dict[int, SurfacePatch]:
    patches: dict[int, SurfacePatch] = {}
    system = context.request.system
    for surface in system.state.surfaces:
        if surface.codev_surface not in system.surface_map:
            continue
        current_radius = _surface_radius_mm(
            optic_object,
            system.surface_map,
            surface.codev_surface,
        )
        current_thickness = _surface_thickness_allow_infinite(
            optic_object,
            system.surface_map,
            surface.codev_surface,
        )
        radius_patch = None
        thickness_patch = None
        if surface.radius_mm is not None and _values_differ(
            surface.radius_mm,
            current_radius,
        ):
            radius_patch = current_radius
        if surface.thickness_mm is not None and _values_differ(
            surface.thickness_mm,
            current_thickness,
        ):
            # Optiland represents CODE V's infinite object/image track as a
            # finite sentinel (normally zero).  That backend representation is
            # not a generated repair and must never overwrite the template.
            if not (
                math.isinf(surface.thickness_mm)
                and math.isfinite(current_thickness)
            ):
                thickness_patch = current_thickness
        if radius_patch is not None or thickness_patch is not None:
            patches[surface.codev_surface] = SurfacePatch(
                radius_mm=radius_patch,
                thickness_mm=thickness_patch,
            )
    return patches


def _candidate_gap_adjustments(
    context: _GenerationContext,
    optic_object: Any,
) -> tuple[GapAdjustment, ...]:
    system = context.request.system
    static_reasons = {
        adjustment.gap_codev_surface: adjustment.reason
        for adjustment in context.baseline.normalization_adjustments
    }
    preceding_gaps = {
        stage.preceding_gap_codev_surface for stage in context.stages
    }
    l1_reconstruction_gaps = {
        stage.preceding_gap_codev_surface
        for stage in context.stages
        if stage.lens.physical_index == 1
    }
    trailing_gaps = {
        block.trailing_gap_codev_surface for block in context.request.repair_blocks
    }
    adjustments: list[GapAdjustment] = []
    for gap_surface, old_gap in context.reference_gaps_mm.items():
        new_gap = _surface_thickness_mm(
            optic_object,
            system.surface_map,
            gap_surface,
        )
        if not _values_differ(old_gap, new_gap):
            continue
        if gap_surface in l1_reconstruction_gaps:
            reason = "PRINCIPAL_PLANE_RECONSTRUCTION_L1_TRAILING"
        elif gap_surface in preceding_gaps:
            reason = "PRINCIPAL_PLANE_RECONSTRUCTION"
        elif gap_surface in trailing_gaps:
            reason = "TRAILING_CLEARANCE_REPAIR"
        else:
            reason = static_reasons.get(
                gap_surface,
                "STATIC_CENTER_AND_EDGE_NORMALIZATION",
            )
        adjustments.append(
            GapAdjustment(
                gap_codev_surface=gap_surface,
                old_gap_mm=old_gap,
                new_gap_mm=new_gap,
                reason=reason,
            )
        )
    return tuple(adjustments)


def _validate_final_node(
    context: _GenerationContext,
    node: _SearchNode,
) -> tuple[Candidate | None, str]:
    system = context.request.system
    settings = context.request.settings
    validation = validate_rebuilt_system(
        node.optic,
        system.lens_map,
        system.surface_map,
        raytrace_num_rays=settings.raytrace_num_rays,
        raytrace_distribution=settings.raytrace_distribution,
    )
    if validation.ray_trace_status is not RayTraceStatus.PASS:
        return None, validation.ray_trace_reason
    return (
        Candidate(
            candidate_id=node.candidate_id,
            optic=node.optic,
            parent_id=node.parent_id,
            lineage=node.lineage,
            surface_patches=_candidate_surface_patches(context, node.optic),
            gap_adjustments=_candidate_gap_adjustments(context, node.optic),
            metrics=node.metrics,
            ray_trace=RayTraceResult(
                status=RayTraceStatus.PASS,
                reason="OK",
            ),
        ),
        "OK",
    )


def generate_thickness_candidates(request: RepairRequest) -> GenerationResult:
    """Run the preserved constant-power and PP-aware branching generator.

    The automatic EFL target is measured after legacy seed-gap normalization.
    Every structurally valid candidate receives full-field, multi-wavelength
    real-ray tracing, and every ray-valid candidate is retained.
    """

    context = _build_generation_context(request)
    settings = request.settings
    root_metrics = _evaluate_candidate_metrics(context, context.search_seed)
    population: list[_SearchNode] = [
        _SearchNode(
            candidate_id="ROOT",
            optic=context.search_seed,
            parent_id=None,
            lineage=(),
            metrics=root_metrics,
        )
    ]
    all_rejections: Counter[str] = Counter()
    stage_summaries: list[GenerationStageSummary] = []
    final_stage_parents: list[_SearchNode] | None = None

    for stage_number, stage in enumerate(context.stages, start=1):
        parents = population
        if stage_number == len(context.stages):
            final_stage_parents = list(parents)
        feasible_children, initial_attempts, rejection_counts = _generate_children(
            context,
            parents,
            stage,
            stage_number,
            settings.branch_factor,
        )
        initial_feasible = len(feasible_children)
        refill_rounds = 0
        refill_attempts = 0
        if (
            stage_number >= settings.population_control_start_stage
            and len(feasible_children) <= settings.min_population
        ):
            while (
                len(feasible_children) < settings.target_population
                and refill_rounds < settings.max_refill_rounds
            ):
                refill_rounds += 1
                additional, attempts, additional_rejections = _generate_children(
                    context,
                    parents,
                    stage,
                    stage_number,
                    settings.refill_samples_per_parent,
                )
                feasible_children.extend(additional)
                refill_attempts += attempts
                rejection_counts.update(additional_rejections)

        population_before_pruning = len(feasible_children)
        pruned_count = 0
        if len(feasible_children) >= settings.max_population:
            keep_indices = context.rng.choice(
                len(feasible_children),
                size=settings.target_population,
                replace=False,
            )
            feasible_children = [
                feasible_children[int(index)] for index in keep_indices
            ]
            pruned_count = population_before_pruning - len(feasible_children)

        all_rejections.update(rejection_counts)
        stage_summaries.append(
            GenerationStageSummary(
                stage_number=stage_number,
                lens_id=stage.lens_id,
                parent_count=len(parents),
                initial_attempts=initial_attempts,
                initial_feasible=initial_feasible,
                refill_rounds=refill_rounds,
                refill_attempts=refill_attempts,
                population_before_pruning=population_before_pruning,
                pruned_count=pruned_count,
                carried_count=len(feasible_children),
                rejection_counts=dict(sorted(rejection_counts.items())),
            )
        )
        if not feasible_children:
            details = ", ".join(
                f"{reason}={count}"
                for reason, count in sorted(rejection_counts.items())
            )
            raise RuntimeError(
                f"Search population collapsed at {stage.lens_id}. "
                f"Rejections: {details or 'none recorded'}."
            )
        population = feasible_children

    final_structural_nodes: list[_SearchNode] = []
    for node in population:
        feasible, reason, metrics = _structural_feasibility_check(
            context,
            node.optic,
            repaired_stage_count=len(context.stages),
        )
        node.metrics = metrics
        if feasible:
            final_structural_nodes.append(node)
        else:
            all_rejections[reason] += 1

    valid_candidates: list[Candidate] = []
    for node in final_structural_nodes:
        candidate, reason = _validate_final_node(context, node)
        if candidate is None:
            all_rejections["RAY_TRACE"] += 1
        else:
            valid_candidates.append(candidate)

    terminal_refill_rounds = 0
    final_stage = context.stages[-1]
    while (
        len(valid_candidates) < settings.target_valid_candidates
        and terminal_refill_rounds < settings.max_terminal_refill_rounds
        and final_stage_parents
    ):
        terminal_refill_rounds += 1
        parent_order = context.rng.permutation(len(final_stage_parents))
        for parent_index in parent_order:
            child, reason = _generate_one_child(
                context,
                final_stage_parents[int(parent_index)],
                final_stage,
                len(context.stages),
            )
            if child is None:
                all_rejections[reason] += 1
                continue
            candidate, ray_reason = _validate_final_node(context, child)
            if candidate is None:
                all_rejections["RAY_TRACE"] += 1
            else:
                valid_candidates.append(candidate)
            if len(valid_candidates) >= settings.target_valid_candidates:
                break

    warnings: list[ParseWarning] = []
    intentionally_unrepaired = sorted(
        context.preexisting_noncompliant_lenses
        - set(request.selected_lenses),
        key=lambda lens_id: request.system.lens_map[lens_id].physical_index,
    )
    if intentionally_unrepaired:
        warnings.append(
            ParseWarning(
                code="UNSELECTED_THICKNESS_VIOLATIONS_RETAINED",
                message=(
                    "The following pre-existing CT/ET violations were not selected "
                    f"and remain unchanged: {', '.join(intentionally_unrepaired)}."
                ),
            )
        )
    if len(valid_candidates) < settings.target_valid_candidates:
        warnings.append(
            ParseWarning(
                code="VALID_CANDIDATE_TARGET_NOT_REACHED",
                message=(
                    f"Generated {len(valid_candidates)} ray-valid candidates; "
                    f"target was {settings.target_valid_candidates}."
                ),
            )
        )
    return GenerationResult(
        request=request,
        baseline=context.baseline,
        valid_candidates=valid_candidates,
        rejection_counts=dict(sorted(all_rejections.items())),
        warnings=warnings,
        stage_summaries=tuple(stage_summaries),
    )


def format_generation_report(result: GenerationResult) -> str:
    """Render baseline, population, rejection, and final-candidate metadata."""

    baseline = result.baseline
    lines = [
        "POWER-PRESERVING THICKNESS GENERATION",
        f"Selected lenses: {', '.join(result.request.selected_lenses)}",
        (
            f"Input EFL / TTL: {baseline.input_efl_mm:.9f} / "
            f"{baseline.input_ttl_mm:.9f} mm"
        ),
        (
            f"Generation EFL target / seed TTL: "
            f"{baseline.generation_efl_mm:.9f} / "
            f"{baseline.generation_ttl_mm:.9f} mm"
        ),
        "Seed-gap normalization:",
    ]
    if baseline.normalization_adjustments:
        for adjustment in baseline.normalization_adjustments:
            lines.append(
                f"  S{adjustment.gap_codev_surface}: "
                f"{adjustment.old_gap_mm:.9f} -> "
                f"{adjustment.new_gap_mm:.9f} mm "
                f"[{adjustment.reason}]"
            )
    else:
        lines.append("  none")

    lines.append("Stages:")
    for summary in result.stage_summaries:
        lines.append(
            f"  {summary.stage_number}. {summary.lens_id}: "
            f"parents={summary.parent_count}, "
            f"attempts={summary.initial_attempts + summary.refill_attempts}, "
            f"feasible={summary.population_before_pruning}, "
            f"pruned={summary.pruned_count}, "
            f"carried={summary.carried_count}"
        )
    lines.append(f"Ray-valid candidates retained: {len(result.valid_candidates)}")
    if result.rejection_counts:
        lines.append("Rejected attempts:")
        for reason, count in result.rejection_counts.items():
            lines.append(f"  {reason}: {count}")
    if result.warnings:
        lines.append("Warnings:")
        for warning in result.warnings:
            lines.append(f"  {warning.code}: {warning.message}")
    if result.valid_candidates:
        lines.extend(
            [
                "Candidates:",
                "ID      EFL [mm]      TTL [mm]      min CT      min ET    min C-gap    min E-gap",
            ]
        )
        for candidate in result.valid_candidates:
            metrics = candidate.metrics
            lines.append(
                f"{candidate.candidate_id:<7} "
                f"{metrics.efl_mm:>12.8f}  "
                f"{metrics.ttl_mm:>12.8f}  "
                f"{metrics.min_ct_mm:>10.6f}  "
                f"{metrics.min_et_mm:>10.6f}  "
                f"{metrics.min_center_gap_mm:>11.7f}  "
                f"{metrics.min_edge_gap_mm:>11.7f}"
            )
    return "\n".join(lines)


def print_generation_report(
    result: GenerationResult,
    output_fn: Callable[[str], Any] = print,
) -> None:
    output_fn(format_generation_report(result))


def save_thickness_diagnosis_layout(
    report: DiagnosisReport,
    output_path: str | Path,
    *,
    num_rays: int = 5,
) -> Path:
    """Save the input layout with CT/ET compliance attached to each lens."""

    label_styles: dict[str, tuple[str, str, str]] = {}
    for row in report.lenses:
        edge_value = (
            f"{row.edge_thickness_mm:.3f}"
            if row.edge_thickness_mm is not None
            else "N/A"
        )
        status_text = "OK" if row.is_compliant else "REPAIR"
        text_color = "#166534" if row.is_compliant else "#B91C1C"
        face_color = "#F0FDF4" if row.is_compliant else "#FEF2F2"
        label_styles[row.lens_id] = (
            (
                f"{row.lens_id}  {status_text}\n"
                f"CT {row.center_thickness_mm:.3f} | ET {edge_value} mm"
            ),
            text_color,
            face_color,
        )

    return _save_labeled_optic_layout(
        report.system.optic,
        report.system.lens_map,
        report.system.surface_map,
        output_path,
        title=f"{report.system.source_path.stem} - thickness diagnosis",
        label_styles=label_styles,
        label_stagger=True,
        num_rays=num_rays,
    )


def save_candidate_layout(
    candidate: Candidate,
    request: RepairRequest,
    output_path: str | Path,
    *,
    num_rays: int = 5,
) -> Path:
    """Save one candidate layout with repaired physical lenses highlighted."""

    selected = set(request.selected_lenses)
    label_styles = {
        lens_id: (
            lens_id,
            "#1D4ED8" if lens_id in selected else "#475569",
            "#EFF6FF" if lens_id in selected else "white",
        )
        for lens_id in request.system.lens_map
    }
    return _save_labeled_optic_layout(
        candidate.optic,
        request.system.lens_map,
        request.system.surface_map,
        output_path,
        title=f"{candidate.candidate_id} - ray-valid thickness candidate",
        label_styles=label_styles,
        num_rays=num_rays,
    )


def _codev_number(value: float) -> str:
    """Return a compact, round-trip-safe CODE V numeric token."""

    numeric = float(value)
    if math.isnan(numeric):
        raise ValueError("A NaN value cannot be exported to CODE V.")
    if math.isinf(numeric):
        return "0"
    if numeric == 0.0:
        return "0"
    return format(numeric, ".17g")


def _same_numeric_value(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    if math.isinf(left) or math.isinf(right):
        return math.isinf(left) and math.isinf(right) and (left > 0) == (right > 0)
    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1.0e-15)


def render_candidate_seq_template(
    system: SystemBundle,
    candidate: Candidate,
) -> tuple[str, dict[tuple[int, str], float]]:
    """Patch only changed radius/thickness tokens in the untouched template.

    The returned change map is used by export round-trip validation. Any stale
    or ambiguous source span stops export rather than reconstructing the line.
    """

    surface_states = {
        surface.codev_surface: surface for surface in system.state.surfaces
    }
    replacements_by_line: dict[int, list[tuple[int, int, str, str]]] = {}
    changed_values: dict[tuple[int, str], float] = {}

    def add_change(
        codev_surface: int,
        attribute: str,
        value: float,
        source: SourceValueRef | None,
    ) -> None:
        if source is None:
            raise ValueError(
                f"S{codev_surface} {attribute} has no unambiguous source token; "
                "template export stopped."
            )
        replacements_by_line.setdefault(source.line_index, []).append(
            (
                source.value_start,
                source.value_end,
                _codev_number(value),
                source.original_token,
            )
        )
        changed_values[(codev_surface, attribute)] = float(value)

    for codev_surface, patch in sorted(candidate.surface_patches.items()):
        state_surface = surface_states.get(codev_surface)
        if state_surface is None:
            raise ValueError(f"Candidate patches unknown CODE V surface S{codev_surface}.")
        if (
            patch.radius_mm is not None
            and not _same_numeric_value(patch.radius_mm, state_surface.radius_mm)
        ):
            add_change(
                codev_surface,
                "radius_mm",
                patch.radius_mm,
                state_surface.radius_source,
            )
        if (
            patch.thickness_mm is not None
            and not _same_numeric_value(patch.thickness_mm, state_surface.thickness_mm)
        ):
            add_change(
                codev_surface,
                "thickness_mm",
                patch.thickness_mm,
                state_surface.thickness_source,
            )

    lines = system.raw_seq_text.splitlines(keepends=True)
    for line_index, replacements in replacements_by_line.items():
        if line_index < 0 or line_index >= len(lines):
            raise ValueError(f"Template source line {line_index + 1} no longer exists.")
        original_line = lines[line_index]
        ordered = sorted(replacements, key=lambda item: item[0])
        previous_end = -1
        for start, end, _replacement, original_token in ordered:
            if start < previous_end:
                raise ValueError(
                    f"Overlapping patch spans on template line {line_index + 1}."
                )
            if original_line[start:end] != original_token:
                raise ValueError(
                    f"Template token drift on line {line_index + 1}: expected "
                    f"{original_token!r}, found {original_line[start:end]!r}."
                )
            previous_end = end
        patched_line = original_line
        for start, end, replacement, _original_token in reversed(ordered):
            patched_line = patched_line[:start] + replacement + patched_line[end:]
        lines[line_index] = patched_line

    return "".join(lines), changed_values


def _encode_like_codev_source(text: str, source_bytes: bytes) -> bytes:
    """Preserve the source sequence encoding while changing numeric tokens."""

    utf8_bom = b"\xef\xbb\xbf"
    if source_bytes.startswith(utf8_bom):
        return utf8_bom + text.encode("utf-8")
    try:
        source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return text.encode("cp1252")
    return text.encode("utf-8")


def _validate_exported_seq(
    seq_path: Path,
    expected_changes: Mapping[tuple[int, str], float],
) -> None:
    """Reparse an exported sequence and verify every changed numeric value."""

    parsed = parse_codev_seq(seq_path)
    surfaces = {surface.codev_surface: surface for surface in parsed.state.surfaces}
    for (codev_surface, attribute), expected in expected_changes.items():
        surface = surfaces.get(codev_surface)
        if surface is None:
            raise ValueError(f"Exported file lost CODE V surface S{codev_surface}.")
        actual = getattr(surface, attribute)
        if not _same_numeric_value(expected, actual):
            raise ValueError(
                f"Export round-trip mismatch for S{codev_surface} {attribute}: "
                f"expected {expected!r}, parsed {actual!r}."
            )


def _csv_number(value: float | None) -> str:
    if value is None:
        return ""
    return format(float(value), ".15g")


def _minimum_or_none(values: Iterable[float]) -> float | None:
    materialized = tuple(float(value) for value in values)
    return min(materialized) if materialized else None


def export_candidates(
    result: GenerationResult,
    output_dir: str | Path = "thickness_repair_output",
    *,
    overwrite: bool = False,
    layout_num_rays: int = 5,
) -> ExportResult:
    """Export every ray-valid candidate without ranking or template rebuilding."""

    output = Path(output_dir).expanduser().resolve()
    candidate_ids = [candidate.candidate_id for candidate in result.valid_candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("Candidate IDs must be unique before export.")
    unsafe_ids = [
        candidate_id
        for candidate_id in candidate_ids
        if re.fullmatch(r"[A-Za-z0-9_-]+", candidate_id) is None
    ]
    if unsafe_ids:
        raise ValueError(f"Unsafe candidate IDs: {', '.join(unsafe_ids)}")

    input_layout_path = output / "00_input_layout.png"
    diagnosis_layout_path = output / "01_thickness_diagnosis.png"
    diagnosis_text_path = output / "diagnosis.txt"
    summary_csv_path = output / "candidate_summary.csv"
    candidate_exports = tuple(
        CandidateExport(
            candidate_id=candidate.candidate_id,
            seq_path=output / f"{candidate.candidate_id}.seq",
            layout_path=output / f"{candidate.candidate_id}_layout.png",
        )
        for candidate in result.valid_candidates
    )
    planned_paths = (
        input_layout_path,
        diagnosis_layout_path,
        diagnosis_text_path,
        summary_csv_path,
        *(path for exported in candidate_exports for path in (exported.seq_path, exported.layout_path)),
    )
    existing = [path for path in planned_paths if path.exists()]
    if existing and not overwrite:
        preview = ", ".join(path.name for path in existing[:5])
        if len(existing) > 5:
            preview += ", ..."
        raise FileExistsError(
            f"Export would overwrite {len(existing)} existing file(s): {preview}. "
            "Choose a new directory or pass overwrite=True."
        )

    source_path = result.request.system.source_path
    source_bytes_before = source_path.read_bytes()
    if _read_seq_text(source_path) != result.request.system.raw_seq_text:
        raise ValueError(
            "The source .seq changed after it was loaded; reload it before export."
        )

    output.mkdir(parents=True, exist_ok=True)
    diagnosis = diagnose_thickness(
        result.request.system,
        result.request.constraints,
    )
    save_physical_lens_layout(
        result.request.system,
        input_layout_path,
        num_rays=layout_num_rays,
    )
    save_thickness_diagnosis_layout(
        diagnosis,
        diagnosis_layout_path,
        num_rays=layout_num_rays,
    )
    diagnosis_text_path.write_text(
        format_diagnosis_report(diagnosis) + "\n",
        encoding="utf-8",
        newline="",
    )

    rows: list[dict[str, str]] = []
    for candidate, exported in zip(result.valid_candidates, candidate_exports):
        patched_text, expected_changes = render_candidate_seq_template(
            result.request.system,
            candidate,
        )
        exported.seq_path.write_bytes(
            _encode_like_codev_source(patched_text, source_bytes_before)
        )
        _validate_exported_seq(exported.seq_path, expected_changes)
        save_candidate_layout(
            candidate,
            result.request,
            exported.layout_path,
            num_rays=layout_num_rays,
        )
        metrics = candidate.metrics
        rows.append(
            {
                "Candidate ID": candidate.candidate_id,
                "EFL [mm]": _csv_number(metrics.efl_mm),
                "TTL [mm]": _csv_number(metrics.ttl_mm),
                "Minimum CT [mm]": _csv_number(
                    _minimum_or_none(metrics.center_thickness_mm.values())
                ),
                "Minimum ET [mm]": _csv_number(
                    _minimum_or_none(metrics.edge_thickness_mm.values())
                ),
                "Minimum center gap [mm]": _csv_number(
                    _minimum_or_none(metrics.center_gaps_mm.values())
                ),
                "Minimum edge gap [mm]": _csv_number(
                    _minimum_or_none(metrics.edge_gaps_mm.values())
                ),
                "Ray trace status": candidate.ray_trace.status.value,
                "Ray trace reason": candidate.ray_trace.reason,
                "Selected lenses": " ".join(result.request.selected_lenses),
                "SEQ file": exported.seq_path.name,
                "Layout file": exported.layout_path.name,
            }
        )

    fieldnames = [
        "Candidate ID",
        "EFL [mm]",
        "TTL [mm]",
        "Minimum CT [mm]",
        "Minimum ET [mm]",
        "Minimum center gap [mm]",
        "Minimum edge gap [mm]",
        "Ray trace status",
        "Ray trace reason",
        "Selected lenses",
        "SEQ file",
        "Layout file",
    ]
    with summary_csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)

    if source_path.read_bytes() != source_bytes_before:
        raise RuntimeError("The original CODE V source changed during export.")

    return ExportResult(
        output_dir=output,
        candidates=candidate_exports,
        summary_csv_path=summary_csv_path,
        input_layout_path=input_layout_path,
        diagnosis_layout_path=diagnosis_layout_path,
        diagnosis_text_path=diagnosis_text_path,
    )


def build_cli_parser() -> argparse.ArgumentParser:
    """Build the direct-run interface without exposing an EFL input."""

    parser = argparse.ArgumentParser(
        description=(
            "CODE V / Optiland physical-lens thickness repair. Running with "
            "no arguments opens an interactive terminal workflow. EFL is "
            "always measured automatically."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python thickness_repair.py\n"
            "  python thickness_repair.py \"input.seq\" --lenses L3 L4 L5\n"
            "  python thickness_repair.py \"input.seq\" --ct-min 1.0 "
            "--et-min 0.2 --center-gap-min 0.1 "
            "--ttl-max 13.5 --non-interactive\n"
        ),
    )
    parser.add_argument(
        "seq_path",
        nargs="?",
        help=(
            "Input CODE V .seq file. Required in non-interactive mode and "
            "prompted for in interactive mode."
        ),
    )
    parser.add_argument("--ct-min", type=float, help="Minimum glass CT [mm].")
    parser.add_argument("--et-min", type=float, help="Minimum glass ET [mm].")
    parser.add_argument(
        "--center-gap-min",
        type=float,
        help="Minimum center air gap [mm].",
    )
    parser.add_argument("--ttl-max", type=float, help="Maximum total thickness [mm].")
    parser.add_argument(
        "--lenses",
        nargs="+",
        metavar="LENS",
        help=(
            "Physical lenses to repair, for example --lenses L3 L4 L5 or "
            "--lenses L3-L7. Omit or press Enter for every noncompliant lens."
        ),
    )
    parser.add_argument(
        "--output-dir",
        help=(
            "Brand-new output directory. It must not already exist. Omit for "
            "an automatically dated folder."
        ),
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Do not prompt. The input .seq path and all four constraint options "
            "are required; missing lens selection means every noncompliant lens."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show a full Python traceback if the run fails.",
    )
    return parser


def _configure_cli_console() -> None:
    """Prefer UTF-8 prompts in Windows IDE terminals when reconfiguration exists."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def _strip_optional_quotes(value: str) -> str:
    cleaned = value.strip()
    if (
        len(cleaned) >= 2
        and cleaned[0] == cleaned[-1]
        and cleaned[0] in {"'", '"'}
    ):
        return cleaned[1:-1].strip()
    return cleaned


def _resolve_cli_path(value: str, *, fallback_directory: Path) -> Path:
    """Resolve pasted/relative paths predictably in an editor terminal."""

    expanded = os.path.expandvars(_strip_optional_quotes(value))
    candidate = Path(expanded).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()

    current_candidate = (Path.cwd() / candidate).resolve()
    if current_candidate.exists():
        return current_candidate
    return (fallback_directory / candidate).resolve()


def _prompt_text(
    prompt: str,
    *,
    default: str | None,
    input_fn: Callable[[str], str],
) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        answer = input_fn(f"{prompt}{suffix}: ").strip()
    except EOFError:
        if default is None:
            raise
        answer = ""
    if answer:
        return answer
    if default is None:
        raise ValueError(f"Missing required input: {prompt}")
    return default


def _prompt_float(
    prompt: str,
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], Any],
) -> float:
    while True:
        try:
            raw = _prompt_text(
                prompt,
                default=None,
                input_fn=input_fn,
            )
            value = float(raw)
        except ValueError:
            output_fn("此项为必填，请输入有限数字，例如 1.0。")
            continue
        if not math.isfinite(value):
            output_fn("数值必须是有限值，不能使用 NaN 或 infinity。")
            continue
        return value


def _finite_cli_value(value: float, name: str) -> float:
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be a finite number.")
    return numeric


def _collect_cli_constraints(
    args: argparse.Namespace,
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], Any],
) -> Constraints:
    definitions = (
        ("ct_min", "Minimum glass center thickness / 最小玻璃中心厚度 [mm]", "--ct-min"),
        ("et_min", "Minimum glass edge thickness / 最小玻璃边缘厚度 [mm]", "--et-min"),
        (
            "center_gap_min",
            "Minimum center air gap / 最小中心空气间隔 [mm]",
            "--center-gap-min",
        ),
        ("ttl_max", "Maximum total thickness / 最大总厚度 [mm]", "--ttl-max"),
    )
    if not interactive:
        missing = [
            option
            for attribute, _prompt, option in definitions
            if getattr(args, attribute) is None
        ]
        if missing:
            raise ValueError(
                "Non-interactive mode requires all four constraints; missing: "
                + ", ".join(missing)
            )

    prompt_every_value = False
    while True:
        values: dict[str, float] = {}
        try:
            for attribute, prompt, _option in definitions:
                supplied = getattr(args, attribute)
                if interactive and (supplied is None or prompt_every_value):
                    values[attribute] = _prompt_float(
                        prompt,
                        input_fn=input_fn,
                        output_fn=output_fn,
                    )
                else:
                    values[attribute] = _finite_cli_value(
                        supplied,
                        attribute,
                    )

            constraints = Constraints(
                ct_min_mm=values["ct_min"],
                et_min_mm=values["et_min"],
                center_gap_min_mm=values["center_gap_min"],
                ttl_max_mm=values["ttl_max"],
            )
        except ValueError as exc:
            if not interactive:
                raise
            prompt_every_value = True
            output_fn(f"约束无效：{exc}")
            output_fn("请重新输入全部四项厚度约束；这些参数没有内置默认值。")
            continue
        return constraints


def _collect_cli_source_path(
    args: argparse.Namespace,
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], Any],
) -> Path:
    script_directory = Path(__file__).resolve().parent
    if args.seq_path is not None:
        candidate_inputs = (args.seq_path,)
    elif not interactive:
        raise ValueError(
            "Non-interactive mode requires the input CODE V .seq path."
        )
    else:
        candidate_inputs = None

    while True:
        if candidate_inputs is None:
            try:
                raw_path = _prompt_text(
                    "CODE V .seq 文件路径（必填）",
                    default=None,
                    input_fn=input_fn,
                )
            except ValueError:
                output_fn("文件路径为必填项，不能留空。")
                continue
        else:
            raw_path = candidate_inputs[0]

        source_path = _resolve_cli_path(
            raw_path,
            fallback_directory=script_directory,
        )
        if source_path.suffix.lower() != ".seq":
            error: Exception = ValueError(
                f"Input must be a CODE V .seq file: {source_path}"
            )
        elif not source_path.is_file():
            error = FileNotFoundError(
                f"Input .seq file does not exist: {source_path}"
            )
        else:
            return source_path

        if candidate_inputs is not None:
            raise error
        output_fn(f"文件无效：{error}")
        output_fn("请重新输入一个现有的 CODE V .seq 文件路径。")


def _automatic_output_dir(
    source_path: Path,
    *,
    now: datetime | None = None,
) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    safe_stem = re.sub(r"[^\w.-]+", "_", source_path.stem).strip("._")
    safe_stem = safe_stem or "codev"
    base_name = f"{safe_stem}_thickness_repair_{timestamp}"
    candidate = source_path.parent / base_name
    serial = 2
    while candidate.exists():
        candidate = source_path.parent / f"{base_name}_{serial:02d}"
        serial += 1
    return candidate.resolve()


def _collect_cli_output_dir(
    args: argparse.Namespace,
    source_path: Path,
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
) -> Path:
    automatic = _automatic_output_dir(source_path)
    if args.output_dir is not None:
        raw_path = args.output_dir
    elif interactive:
        raw_path = _prompt_text(
            "输出文件夹（将保存全部 .seq、layout 和 summary CSV）",
            default=str(automatic),
            input_fn=input_fn,
        )
    else:
        raw_path = str(automatic)

    output_path = _resolve_cli_path(
        raw_path,
        fallback_directory=source_path.parent,
    )
    if output_path.exists():
        raise FileExistsError(
            f"Output path already exists: {output_path}. Choose a new folder; "
            "the command-line workflow never overwrites an existing path."
        )
    return output_path


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _raise_for_error_warnings(
    warnings: Iterable[ParseWarning],
    *,
    stage: str,
) -> None:
    """Turn structured ERROR diagnostics into a safe CLI stop."""

    errors = tuple(
        warning for warning in warnings if warning.severity is WarningSeverity.ERROR
    )
    if not errors:
        return
    details = "; ".join(
        f"{warning.code}: {warning.message}" for warning in errors
    )
    raise RuntimeError(f"{stage} reported hard errors: {details}")


def _collect_cli_repair_request(
    args: argparse.Namespace,
    diagnosis: DiagnosisReport,
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], Any],
) -> tuple[RepairRequest | None, str]:
    """Collect and validate lens selection, re-prompting only in interactive use."""

    settings = GenerationSettings()
    if args.lenses is not None:
        selection_text = " ".join(args.lenses)
        return (
            select_lenses_for_repair(
                diagnosis,
                selection_text,
                settings=settings,
            ),
            selection_text,
        )
    if not interactive:
        selection_text = ""
        return (
            select_lenses_for_repair(
                diagnosis,
                selection_text,
                settings=settings,
            ),
            selection_text,
        )

    default_lenses = ", ".join(diagnosis.noncompliant_lenses) or "none"
    while True:
        selection_text = _prompt_text(
            (
                "选择要修复的镜片（例如 L3 L4 或 L3-L7；"
                f"Enter=全部不合格镜片 {default_lenses}；NONE=取消）"
            ),
            default="",
            input_fn=input_fn,
        )
        try:
            request = select_lenses_for_repair(
                diagnosis,
                selection_text,
                settings=settings,
            )
        except ValueError as exc:
            output_fn(f"选择无效：{exc}")
            continue
        return request, selection_text


def _format_cli_run_report(
    result: GenerationResult,
    source_hash: str,
) -> str:
    constraints = result.request.constraints
    settings = result.request.settings
    seed_description = (
        "system entropy (random per run)"
        if settings.random_seed is None
        else str(settings.random_seed)
    )
    lines = [
        "CODE V THICKNESS-REPAIR RUN",
        f"Source: {result.request.system.source_path.name}",
        f"Source SHA256: {source_hash}",
        f"Selected lenses: {', '.join(result.request.selected_lenses)}",
        (
            "Constraints [mm]: "
            f"CT>={constraints.ct_min_mm:g}; "
            f"ET>={constraints.et_min_mm:g}; "
            f"center air gap>={constraints.center_gap_min_mm:g}; "
            f"TTL<={constraints.ttl_max_mm:g}"
        ),
        "EFL: measured automatically; no user-entered target",
        (
            "Generation settings: "
            f"branch={settings.branch_factor}; "
            f"population={settings.min_population}/"
            f"{settings.target_population}/{settings.max_population}; "
            f"seed={seed_description}"
        ),
        (
            "Real-ray validation: "
            f"{settings.raytrace_num_rays} rays; "
            f"{settings.raytrace_distribution}; all fields and wavelengths"
        ),
        "",
        format_generation_report(result),
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def run_cli_workflow(
    args: argparse.Namespace,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> ExportResult | None:
    """Run the full parser-to-export workflow for the command-line interface."""

    interactive = not args.non_interactive
    source_path = _collect_cli_source_path(
        args,
        interactive=interactive,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    constraints = _collect_cli_constraints(
        args,
        interactive=interactive,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    source_hash_before = _file_sha256(source_path)

    output_fn("")
    output_fn("[1/5] Loading CODE V and rebuilding in Optiland...")
    system = load_codev_seq(source_path)
    validation = system.validation
    if validation is None:
        raise RuntimeError("Optiland baseline validation did not run.")
    for name, value in (("EFL", validation.efl_mm), ("TTL", validation.ttl_mm)):
        if not math.isfinite(float(value)):
            raise RuntimeError(f"The input {name} is not finite: {value!r}")
    if validation.ray_trace_status is not RayTraceStatus.PASS:
        raise RuntimeError(
            "The input system failed baseline real-ray tracing: "
            f"{validation.ray_trace_reason}"
        )
    output_fn(
        f"Input EFL (automatic): {validation.efl_mm:.9f} mm; "
        f"TTL: {validation.ttl_mm:.9f} mm; "
        f"fields: {len(system.fields)}; wavelengths: {len(system.wavelengths)}"
    )
    for warning in system.warnings:
        output_fn(f"{warning.severity.value}: {warning.code}: {warning.message}")
    _raise_for_error_warnings(system.warnings, stage="CODE V / Optiland load")

    output_fn("")
    output_fn("[2/5] Diagnosing physical-lens thickness...")
    diagnosis = diagnose_thickness(system, constraints)
    output_fn(format_diagnosis_report(diagnosis))
    if diagnosis.total_thickness_mm is None or not math.isfinite(
        float(diagnosis.total_thickness_mm)
    ):
        raise RuntimeError("The diagnosed total thickness is unavailable or non-finite.")
    unavailable_edges = tuple(
        row.lens_id for row in diagnosis.lenses if row.edge_status is None
    )
    if unavailable_edges:
        raise RuntimeError(
            "Edge thickness could not be evaluated for: "
            + ", ".join(unavailable_edges)
        )
    _raise_for_error_warnings(diagnosis.warnings, stage="Thickness diagnosis")

    request, selection_text = _collect_cli_repair_request(
        args,
        diagnosis,
        interactive=interactive,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    if request is None:
        if not selection_text.strip() and not diagnosis.noncompliant_lenses:
            output_fn("没有发现 CT/ET 不合格镜片；无需修复，也没有创建候选文件。")
        else:
            output_fn("用户取消了镜片修复；没有创建候选文件。")
        return None

    output_path = _collect_cli_output_dir(
        args,
        source_path,
        interactive=interactive,
        input_fn=input_fn,
    )
    output_path.mkdir(parents=True, exist_ok=False)
    output_fn("")
    output_fn(f"Selected lenses: {', '.join(request.selected_lenses)}")
    output_fn(f"Output directory: {output_path}")
    output_fn("EFL will be measured automatically after legacy normalization.")

    output_fn("")
    output_fn(
        "[3/5] Running the preserved full PP search and 12-ray validation. "
        "This can take several minutes..."
    )
    try:
        result = generate_thickness_candidates(request)
        if not result.valid_candidates:
            raise RuntimeError("No ray-valid candidates were generated.")
        output_fn(f"Ray-valid candidates retained: {len(result.valid_candidates)}")

        output_fn("")
        output_fn("[4/5] Exporting every candidate .seq and physical-lens layout...")
        export_result = export_candidates(
            result,
            output_path,
            overwrite=False,
            layout_num_rays=DEFAULT_CLI_LAYOUT_NUM_RAYS,
        )
        report_path = export_result.output_dir / "generation_report.txt"
        with report_path.open("x", encoding="utf-8", newline="") as stream:
            stream.write(_format_cli_run_report(result, source_hash_before))
    except Exception:
        try:
            output_path.rmdir()
        except OSError:
            pass
        raise

    source_hash_after = _file_sha256(source_path)
    if source_hash_after != source_hash_before:
        raise RuntimeError("The original .seq changed during the CLI run.")

    output_fn("")
    output_fn("[5/5] Complete.")
    output_fn(f"Candidates: {len(export_result.candidates)}")
    output_fn(f"Summary CSV: {export_result.summary_csv_path}")
    output_fn(f"Run report: {report_path}")
    output_fn(f"Original .seq unchanged (SHA256): {source_hash_after}")
    return export_result


def main(
    argv: Sequence[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
    error_fn: Callable[[str], Any] | None = None,
) -> int:
    """Run interactively from an editor or non-interactively from a terminal."""

    _configure_cli_console()
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    if error_fn is None:
        error_fn = lambda message: print(message, file=sys.stderr)
    try:
        run_cli_workflow(args, input_fn=input_fn, output_fn=output_fn)
    except KeyboardInterrupt:
        error_fn("\n运行已由用户取消。")
        return 130
    except Exception as exc:
        if args.debug:
            raise
        error_fn(f"\n运行失败：{exc}")
        error_fn("可使用 --help 查看参数，或添加 --debug 显示完整错误信息。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
