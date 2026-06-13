#!/usr/bin/env python3
"""
Rhythmbox playlist maintenance and Flacbox M3U/M3U8 export.

Subcommands:
  tags     List detected genre tags.
  backup   Back up playlists.xml and rhythmdb.xml while excluding selected derived playlists.
  derive   Detect genre tags and create/replace derived Rhythmbox static playlists.
  flacbox  Export Rhythmbox playlists to Flacbox-compatible M3U/M3U8 files.

Interactive tag selection uses the optional third-party package questionary:
  python -m pip install questionary

Non-interactive usage does not require questionary; pass --tag, --tag-file, or --all-tags.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime
import json
import pathlib
import re
import shutil
import sys
import tempfile
from typing import Any, Sequence
import urllib.parse
import xml.etree.ElementTree as ET


DEFAULT_SOURCE_PLAYLIST: str = "dev"
DEFAULT_TAG_SEPARATORS: str = ";,"
DEFAULT_PLAYLIST_NAME_TEMPLATE: str = "{tag}"
UNSAFE_FILENAME_RE: re.Pattern[str] = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
WHITESPACE_RE: re.Pattern[str] = re.compile(r"\s+")


@dataclasses.dataclass(frozen=True)
class Song:
    location: str
    title: str = ""
    genre: str = ""
    artist: str = ""
    album: str = ""
    duration: int | None = None
    file_size: int | None = None
    media_type: str = ""
    composer: str = ""


@dataclasses.dataclass(frozen=True)
class PlaylistData:
    name: str
    locations: tuple[str, ...]
    playlist_type: str = "static"


@dataclasses.dataclass(frozen=True)
class TagStat:
    tag: str
    track_count: int


@dataclasses.dataclass(frozen=True)
class PathMapping:
    path_mode: str
    music_root: pathlib.Path | None
    path_prefix: str
    allow_outside_root: bool


class PlaylistToolError(RuntimeError):
    pass


def parse_xml_file(path: pathlib.Path) -> ET.ElementTree:
    try:
        return ET.parse(path)
    except FileNotFoundError as exc:
        raise PlaylistToolError(f"File does not exist: {path}") from exc
    except ET.ParseError as exc:
        raise PlaylistToolError(f"Invalid XML in {path}: {exc}") from exc


def child_text(element: ET.Element, child_name: str, default: str = "") -> str:
    child: ET.Element | None = element.find(child_name)
    if child is None or child.text is None:
        return default
    return child.text


def parse_int(value: str) -> int | None:
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def read_rhythmdb(db_xml: pathlib.Path) -> dict[str, Song]:
    tree: ET.ElementTree = parse_xml_file(db_xml)
    root: ET.Element = tree.getroot()
    songs_by_location: dict[str, Song] = {}
    for entry in root.findall("entry"):
        location: str = child_text(entry, "location")
        if location == "":
            continue
        songs_by_location[location] = Song(
            location=location,
            title=child_text(entry, "title"),
            genre=child_text(entry, "genre"),
            artist=child_text(entry, "artist"),
            album=child_text(entry, "album"),
            duration=parse_int(child_text(entry, "duration")),
            file_size=parse_int(child_text(entry, "file-size")),
            media_type=child_text(entry, "media-type"),
            composer=child_text(entry, "composer"),
        )
    return songs_by_location


def read_playlists(playlist_xml: pathlib.Path, static_only: bool = True) -> dict[str, PlaylistData]:
    tree: ET.ElementTree = parse_xml_file(playlist_xml)
    root: ET.Element = tree.getroot()
    playlists: dict[str, PlaylistData] = {}
    for playlist_element in root.findall("playlist"):
        playlist_name: str = playlist_element.attrib.get("name", "")
        playlist_type: str = playlist_element.attrib.get("type", "")
        if playlist_name == "":
            continue
        if static_only and playlist_type != "static":
            continue
        locations: list[str] = []
        for location_element in playlist_element.findall("location"):
            if location_element.text is not None:
                locations.append(location_element.text)
        playlists[playlist_name] = PlaylistData(
            name=playlist_name,
            locations=tuple(locations),
            playlist_type=playlist_type,
        )
    return playlists


def normalize_tag_text(tag: str, case_sensitive: bool) -> str:
    stripped_tag: str = tag.strip()
    if case_sensitive:
        return stripped_tag
    return stripped_tag.casefold()


def split_genre_tags(genre: str, tag_separators: str) -> list[str]:
    stripped_genre: str = genre.strip()
    if stripped_genre == "":
        return []
    if tag_separators == "":
        return [stripped_genre]
    split_pattern: str = f"[{re.escape(tag_separators)}]+"
    tokens: list[str] = [token.strip() for token in re.split(split_pattern, stripped_genre)]
    return [token for token in tokens if token != ""]


def discover_genre_tags(
    songs: Sequence[Song],
    tag_separators: str,
    case_sensitive: bool,
    sort_mode: str,
) -> list[TagStat]:
    counts: collections.Counter[str] = collections.Counter()
    display_by_key: dict[str, str] = {}
    for song in songs:
        per_song_keys: set[str] = set()
        for tag in split_genre_tags(song.genre, tag_separators):
            normalized_tag: str = normalize_tag_text(tag, case_sensitive)
            if normalized_tag == "":
                continue
            per_song_keys.add(normalized_tag)
            display_by_key.setdefault(normalized_tag, tag.strip())
        for normalized_tag in per_song_keys:
            counts[normalized_tag] += 1
    stats: list[TagStat] = [
        TagStat(tag=display_by_key[key], track_count=count)
        for key, count in counts.items()
    ]
    if sort_mode == "count":
        return sorted(stats, key=lambda stat: (-stat.track_count, stat.tag.casefold()))
    if sort_mode == "name":
        return sorted(stats, key=lambda stat: stat.tag.casefold())
    raise PlaylistToolError(f"Unsupported tag sort mode: {sort_mode}")


def song_has_tag(song: Song, selected_tag: str, tag_separators: str, case_sensitive: bool) -> bool:
    selected_key: str = normalize_tag_text(selected_tag, case_sensitive)
    token_keys: set[str] = {
        normalize_tag_text(token, case_sensitive)
        for token in split_genre_tags(song.genre, tag_separators)
    }
    return selected_key in token_keys


def songs_from_locations(
    locations: Sequence[str],
    songs_by_location: dict[str, Song],
    strict: bool,
) -> tuple[list[Song], list[str]]:
    songs: list[Song] = []
    missing_locations: list[str] = []
    for location in locations:
        song: Song | None = songs_by_location.get(location)
        if song is None:
            missing_locations.append(location)
            if strict:
                raise PlaylistToolError(f"Playlist location not found in rhythmdb.xml: {location}")
            continue
        songs.append(song)
    return songs, missing_locations


def load_source_playlist_songs(
    playlist_xml: pathlib.Path,
    db_xml: pathlib.Path,
    source_playlist: str,
    strict: bool,
) -> tuple[PlaylistData, list[Song], list[str], dict[str, PlaylistData], dict[str, Song]]:
    playlists: dict[str, PlaylistData] = read_playlists(playlist_xml, static_only=True)
    source_data: PlaylistData | None = playlists.get(source_playlist)
    if source_data is None:
        available_names: str = ", ".join(sorted(playlists.keys()))
        raise PlaylistToolError(
            f"Source playlist {source_playlist!r} not found. Available static playlists: {available_names}"
        )
    songs_by_location: dict[str, Song] = read_rhythmdb(db_xml)
    source_songs: list[Song]
    missing_locations: list[str]
    source_songs, missing_locations = songs_from_locations(
        source_data.locations,
        songs_by_location,
        strict=strict,
    )
    return source_data, source_songs, missing_locations, playlists, songs_by_location


def load_tag_scope_songs(
    playlist_xml: pathlib.Path,
    db_xml: pathlib.Path,
    source_playlist: str,
    tag_scope: str,
    strict: bool,
) -> tuple[list[Song], str, list[str], dict[str, PlaylistData]]:
    if tag_scope == "database":
        songs_by_location: dict[str, Song] = read_rhythmdb(db_xml)
        playlists: dict[str, PlaylistData] = read_playlists(playlist_xml, static_only=True)
        return list(songs_by_location.values()), "database", [], playlists
    if tag_scope == "source":
        source_data: PlaylistData
        songs: list[Song]
        missing_locations: list[str]
        playlists: dict[str, PlaylistData]
        source_data, songs, missing_locations, playlists, _ = load_source_playlist_songs(
            playlist_xml=playlist_xml,
            db_xml=db_xml,
            source_playlist=source_playlist,
            strict=strict,
        )
        return songs, f"source playlist {source_data.name!r}", missing_locations, playlists
    raise PlaylistToolError(f"Unsupported tag scope: {tag_scope}")


def render_playlist_name(tag: str, template: str) -> str:
    safe_tag: str = WHITESPACE_RE.sub(" ", tag.strip())
    try:
        playlist_name: str = template.format(tag=tag, safe_tag=safe_tag)
    except (KeyError, IndexError, ValueError) as exc:
        raise PlaylistToolError(
            f"Invalid --playlist-name-template {template!r}. Use {{tag}} or {{safe_tag}}."
        ) from exc
    playlist_name = playlist_name.strip()
    if playlist_name == "":
        raise PlaylistToolError(f"Tag {tag!r} maps to an empty playlist name.")
    return playlist_name


def playlist_names_from_tags(tags: Sequence[str], template: str) -> set[str]:
    return {render_playlist_name(tag, template) for tag in tags}


def unique_preserving_order(values: Sequence[str], case_sensitive: bool) -> list[str]:
    seen_keys: set[str] = set()
    unique_values: list[str] = []
    for value in values:
        stripped_value: str = value.strip()
        if stripped_value == "":
            continue
        key: str = normalize_tag_text(stripped_value, case_sensitive)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_values.append(stripped_value)
    return unique_values


def load_tag_file(tag_file: pathlib.Path) -> list[str]:
    try:
        text: str = tag_file.expanduser().read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PlaylistToolError(f"Tag file does not exist: {tag_file}") from exc
    tags: list[str] = []
    for line in text.splitlines():
        cleaned_line: str = line.strip()
        if cleaned_line == "" or cleaned_line.startswith("#"):
            continue
        tags.append(cleaned_line)
    if len(tags) == 0:
        raise PlaylistToolError(f"Tag file contains no tags: {tag_file}")
    return tags


def write_text_file_atomic(output_path: pathlib.Path, text: str, overwrite: bool) -> None:
    if output_path.exists() and not overwrite:
        raise PlaylistToolError(f"Refusing to overwrite existing file: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=str(output_path.parent),
        delete=False,
    ) as temp_file:
        temp_path: pathlib.Path = pathlib.Path(temp_file.name)
        temp_file.write(text)
    temp_path.replace(output_path)


def write_tag_file(tag_file: pathlib.Path, tags: Sequence[str], force: bool) -> None:
    text: str = "".join(f"{tag}\n" for tag in tags)
    write_text_file_atomic(tag_file.expanduser().resolve(), text, overwrite=force)


def canonicalize_selected_tags(
    selected_tags: Sequence[str],
    tag_stats: Sequence[TagStat],
    case_sensitive: bool,
) -> tuple[list[str], list[str]]:
    lookup: dict[str, str] = {
        normalize_tag_text(stat.tag, case_sensitive): stat.tag for stat in tag_stats
    }
    canonical_tags: list[str] = []
    unknown_tags: list[str] = []
    for selected_tag in selected_tags:
        key: str = normalize_tag_text(selected_tag, case_sensitive)
        if key in lookup:
            canonical_tags.append(lookup[key])
        else:
            canonical_tags.append(selected_tag)
            unknown_tags.append(selected_tag)
    return (
        unique_preserving_order(canonical_tags, case_sensitive),
        unique_preserving_order(unknown_tags, case_sensitive),
    )


def load_questionary_module() -> Any:
    try:
        import questionary  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PlaylistToolError(
            "Interactive tag selection requires questionary. Install it with: "
            "python -m pip install questionary. Or use --tag, --tag-file, or --all-tags."
        ) from exc
    return questionary


def select_tags_interactively(
    tag_stats: Sequence[TagStat],
    preselected_tags: set[str],
    case_sensitive: bool,
    message: str,
) -> list[str]:
    if len(tag_stats) == 0:
        raise PlaylistToolError("No genre tags were discovered.")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise PlaylistToolError(
            "Interactive tag selection needs a terminal. Use --tag, --tag-file, or --all-tags instead."
        )
    questionary: Any = load_questionary_module()
    choice_class: Any = getattr(questionary, "Choice")
    normalized_preselected: set[str] = {
        normalize_tag_text(tag, case_sensitive) for tag in preselected_tags
    }
    choices: list[Any] = []
    for stat in tag_stats:
        checked: bool = normalize_tag_text(stat.tag, case_sensitive) in normalized_preselected
        label: str = f"{stat.tag} ({stat.track_count} track{'s' if stat.track_count != 1 else ''})"
        choices.append(choice_class(title=label, value=stat.tag, checked=checked))
    selected: Any = questionary.checkbox(
        message,
        choices=choices,
        validate=lambda value: True if len(value) > 0 else "Select at least one tag.",
        instruction="Use Space to toggle, Enter to accept.",
    ).ask()
    if selected is None:
        raise PlaylistToolError("Tag selection was cancelled.")
    if not isinstance(selected, list):
        raise PlaylistToolError("Tag selection returned an unexpected value.")
    return [str(value) for value in selected]


def resolve_selected_tags(
    args: argparse.Namespace,
    tag_stats: Sequence[TagStat],
    preselected_tags: set[str],
    message: str,
) -> tuple[list[str], list[str]]:
    selected_tags: list[str] = []
    if args.all_tags:
        selected_tags.extend([stat.tag for stat in tag_stats])
    if args.tag_file is not None:
        selected_tags.extend(load_tag_file(args.tag_file))
    if args.tag:
        selected_tags.extend(args.tag)
    if len(selected_tags) == 0:
        if args.no_interactive:
            raise PlaylistToolError(
                "No tags selected. Use --tag, --tag-file, --all-tags, or remove --no-interactive."
            )
        selected_tags = select_tags_interactively(
            tag_stats=tag_stats,
            preselected_tags=preselected_tags,
            case_sensitive=args.case_sensitive_tags,
            message=message,
        )
    selected_tags = unique_preserving_order(selected_tags, args.case_sensitive_tags)
    if args.exclude_tag:
        excluded_keys: set[str] = {
            normalize_tag_text(tag, args.case_sensitive_tags) for tag in args.exclude_tag
        }
        selected_tags = [
            tag for tag in selected_tags
            if normalize_tag_text(tag, args.case_sensitive_tags) not in excluded_keys
        ]
    selected_tags, unknown_tags = canonicalize_selected_tags(
        selected_tags=selected_tags,
        tag_stats=tag_stats,
        case_sensitive=args.case_sensitive_tags,
    )
    if len(selected_tags) == 0:
        raise PlaylistToolError("No tags selected after applying exclusions.")
    if args.write_tag_file is not None:
        write_tag_file(args.write_tag_file, selected_tags, force=args.force_tag_file)
    return selected_tags, unknown_tags


def print_tag_stats(tag_stats: Sequence[TagStat], existing_playlist_names: set[str], template: str) -> None:
    for stat in tag_stats:
        playlist_name: str = render_playlist_name(stat.tag, template)
        exists_marker: str = "exists" if playlist_name in existing_playlist_names else "new"
        print(f"{stat.tag}\t{stat.track_count}\t{playlist_name}\t{exists_marker}")


def print_unknown_tag_warning(unknown_tags: Sequence[str]) -> None:
    if len(unknown_tags) == 0:
        return
    print(
        "Warning: selected tag(s) were not discovered in the selected tag scope: "
        + ", ".join(unknown_tags),
        file=sys.stderr,
    )


def build_playlist_element(name: str, locations: Sequence[str], browser_position: str = "0") -> ET.Element:
    playlist_element: ET.Element = ET.Element(
        "playlist",
        {
            "name": name,
            "show-browser": "false",
            "browser-position": browser_position,
            "search-type": "search-match",
            "type": "static",
        },
    )
    for location in locations:
        location_element: ET.Element = ET.SubElement(playlist_element, "location")
        location_element.text = location
    return playlist_element


def remove_static_playlists_by_name(root: ET.Element, names: set[str]) -> int:
    removed_count: int = 0
    for playlist_element in list(root):
        if playlist_element.tag != "playlist":
            continue
        playlist_name: str = playlist_element.attrib.get("name", "")
        playlist_type: str = playlist_element.attrib.get("type", "")
        if playlist_type == "static" and playlist_name in names:
            root.remove(playlist_element)
            removed_count += 1
    return removed_count


def find_insert_index_after_playlist(root: ET.Element, playlist_name: str) -> int:
    children: list[ET.Element] = list(root)
    for index, child in enumerate(children):
        if child.tag == "playlist" and child.attrib.get("name", "") == playlist_name:
            return index + 1
    return len(children)


def write_xml_atomic(tree: ET.ElementTree, output_path: pathlib.Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    root: ET.Element = tree.getroot()
    ET.indent(tree, space="  ")
    xml_body: str = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    xml_text: str = '<?xml version="1.0" standalone="yes"?>\n' + xml_body + "\n"
    write_text_file_atomic(output_path, xml_text, overwrite=True)


def copy_file_no_overwrite(source: pathlib.Path, destination: pathlib.Path, force: bool) -> None:
    if destination.exists() and not force:
        raise PlaylistToolError(f"Backup already exists: {destination}. Use --force to overwrite.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def command_tags(args: argparse.Namespace) -> int:
    playlist_xml: pathlib.Path = args.playlist_xml.expanduser().resolve()
    db_xml: pathlib.Path = args.db_xml.expanduser().resolve()
    songs: list[Song]
    scope_label: str
    missing_locations: list[str]
    playlists: dict[str, PlaylistData]
    songs, scope_label, missing_locations, playlists = load_tag_scope_songs(
        playlist_xml=playlist_xml,
        db_xml=db_xml,
        source_playlist=args.source_playlist,
        tag_scope=args.tag_scope,
        strict=args.strict,
    )
    tag_stats: list[TagStat] = discover_genre_tags(
        songs=songs,
        tag_separators=args.tag_separators,
        case_sensitive=args.case_sensitive_tags,
        sort_mode=args.tag_sort,
    )
    existing_names: set[str] = set(playlists.keys())
    if args.format == "json":
        payload: list[dict[str, str | int]] = [
            {
                "tag": stat.tag,
                "track_count": stat.track_count,
                "playlist_name": render_playlist_name(stat.tag, args.playlist_name_template),
                "playlist_exists": render_playlist_name(stat.tag, args.playlist_name_template) in existing_names,
            }
            for stat in tag_stats
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.format == "plain":
        for stat in tag_stats:
            print(stat.tag)
    else:
        print(f"Discovered {len(tag_stats)} tag(s) from {scope_label} ({len(songs)} track(s)).")
        print("tag\ttrack_count\tplaylist_name\tstatus")
        print_tag_stats(tag_stats, existing_names, args.playlist_name_template)
    if missing_locations:
        print(f"Warning: skipped {len(missing_locations)} source location(s) missing from rhythmdb.xml")
    return 0


def command_backup(args: argparse.Namespace) -> int:
    playlist_xml: pathlib.Path = args.playlist_xml.expanduser().resolve()
    db_xml: pathlib.Path = args.db_xml.expanduser().resolve()
    backup_dir: pathlib.Path = args.backup_dir.expanduser().resolve()
    date_stamp: str = args.date_stamp or datetime.date.today().isoformat()
    songs: list[Song]
    scope_label: str
    missing_locations: list[str]
    playlists: dict[str, PlaylistData]
    songs, scope_label, missing_locations, playlists = load_tag_scope_songs(
        playlist_xml=playlist_xml,
        db_xml=db_xml,
        source_playlist=args.source_playlist,
        tag_scope=args.tag_scope,
        strict=args.strict,
    )
    tag_stats: list[TagStat] = discover_genre_tags(
        songs=songs,
        tag_separators=args.tag_separators,
        case_sensitive=args.case_sensitive_tags,
        sort_mode=args.tag_sort,
    )
    existing_names: set[str] = set(playlists.keys())
    preselected_tags: set[str] = {
        stat.tag for stat in tag_stats
        if render_playlist_name(stat.tag, args.playlist_name_template) in existing_names
    }
    selected_tags: list[str]
    unknown_tags: list[str]
    selected_tags, unknown_tags = resolve_selected_tags(
        args=args,
        tag_stats=tag_stats,
        preselected_tags=preselected_tags,
        message="Select derived genre-tag playlists to exclude from the backup",
    )
    print_unknown_tag_warning(unknown_tags)
    excluded_playlist_names: set[str] = playlist_names_from_tags(selected_tags, args.playlist_name_template)
    if args.exclude_playlist:
        excluded_playlist_names.update(args.exclude_playlist)
    playlists_backup_path: pathlib.Path = backup_dir / f"playlists-{date_stamp}.xml"
    db_backup_path: pathlib.Path = backup_dir / f"rhythmdb-{date_stamp}.xml"
    if playlists_backup_path.exists() and not args.force:
        raise PlaylistToolError(f"Backup already exists: {playlists_backup_path}. Use --force to overwrite.")
    if db_backup_path.exists() and not args.force:
        raise PlaylistToolError(f"Backup already exists: {db_backup_path}. Use --force to overwrite.")
    tree: ET.ElementTree = parse_xml_file(playlist_xml)
    root: ET.Element = tree.getroot()
    removed_count: int = remove_static_playlists_by_name(root, excluded_playlist_names)
    write_xml_atomic(tree, playlists_backup_path)
    copy_file_no_overwrite(db_xml, db_backup_path, force=args.force)
    print(f"Wrote playlist backup without selected derived playlists: {playlists_backup_path}")
    print(f"Wrote rhythmdb backup: {db_backup_path}")
    print(f"Tag discovery scope: {scope_label} ({len(songs)} track(s))")
    if missing_locations:
        print(f"Warning: skipped {len(missing_locations)} source location(s) missing from rhythmdb.xml")
    print("Selected tags: " + ", ".join(selected_tags))
    print("Excluded playlist names: " + ", ".join(sorted(excluded_playlist_names, key=str.casefold)))
    print(f"Removed {removed_count} derived playlist(s) from the playlist backup only.")
    return 0


def command_derive(args: argparse.Namespace) -> int:
    playlist_xml: pathlib.Path = args.playlist_xml.expanduser().resolve()
    db_xml: pathlib.Path = args.db_xml.expanduser().resolve()
    output_path: pathlib.Path = playlist_xml if args.output is None else args.output.expanduser().resolve()
    source_data: PlaylistData
    source_songs: list[Song]
    missing_locations: list[str]
    playlists: dict[str, PlaylistData]
    source_data, source_songs, missing_locations, playlists, _ = load_source_playlist_songs(
        playlist_xml=playlist_xml,
        db_xml=db_xml,
        source_playlist=args.source_playlist,
        strict=args.strict,
    )
    tag_stats: list[TagStat] = discover_genre_tags(
        songs=source_songs,
        tag_separators=args.tag_separators,
        case_sensitive=args.case_sensitive_tags,
        sort_mode=args.tag_sort,
    )
    existing_names: set[str] = set(playlists.keys())
    preselected_tags: set[str] = {
        stat.tag for stat in tag_stats
        if render_playlist_name(stat.tag, args.playlist_name_template) in existing_names
    }
    selected_tags: list[str]
    unknown_tags: list[str]
    selected_tags, unknown_tags = resolve_selected_tags(
        args=args,
        tag_stats=tag_stats,
        preselected_tags=preselected_tags,
        message=f"Select genre tags to derive from source playlist {args.source_playlist!r}",
    )
    print_unknown_tag_warning(unknown_tags)
    selected_playlist_names: set[str] = playlist_names_from_tags(selected_tags, args.playlist_name_template)
    if args.source_playlist in selected_playlist_names and not args.allow_source_overwrite:
        raise PlaylistToolError(
            f"A selected tag maps to the source playlist name {args.source_playlist!r}. "
            "Use --playlist-name-template or --allow-source-overwrite."
        )
    names_to_remove: set[str] = set(selected_playlist_names)
    if args.prune_unselected_tags:
        names_to_remove.update(
            playlist_names_from_tags([stat.tag for stat in tag_stats], args.playlist_name_template)
        )
    tree: ET.ElementTree = parse_xml_file(playlist_xml)
    root: ET.Element = tree.getroot()
    insert_index: int = find_insert_index_after_playlist(root, args.source_playlist)
    removed_count: int = remove_static_playlists_by_name(root, names_to_remove)
    new_elements: list[ET.Element] = []
    created_counts: dict[str, int] = {}
    for selected_tag in selected_tags:
        playlist_name: str = render_playlist_name(selected_tag, args.playlist_name_template)
        matched_locations: list[str] = [
            song.location
            for song in source_songs
            if song_has_tag(
                song=song,
                selected_tag=selected_tag,
                tag_separators=args.tag_separators,
                case_sensitive=args.case_sensitive_tags,
            )
        ]
        created_counts[playlist_name] = len(matched_locations)
        new_elements.append(build_playlist_element(playlist_name, matched_locations))
    for offset, playlist_element in enumerate(new_elements):
        root.insert(insert_index + offset, playlist_element)
    write_xml_atomic(tree, output_path)
    print(f"Wrote derived playlists to: {output_path}")
    print(f"Source playlist: {source_data.name} ({len(source_data.locations)} location(s))")
    print(f"Matched source songs present in rhythmdb.xml: {len(source_songs)}")
    if missing_locations:
        print(f"Warning: skipped {len(missing_locations)} source location(s) missing from rhythmdb.xml")
    print("Selected tags: " + ", ".join(selected_tags))
    print(f"Removed {removed_count} old/pruned derived playlist(s).")
    for playlist_name, count in created_counts.items():
        print(f"Created/updated {playlist_name}: {count} track(s)")
    return 0


def rhythmdb_location_to_local_path(location: str) -> pathlib.Path | None:
    parsed: urllib.parse.ParseResult = urllib.parse.urlparse(location)
    if parsed.scheme == "file":
        return pathlib.Path(urllib.parse.unquote(parsed.path))
    if parsed.scheme == "":
        return pathlib.Path(urllib.parse.unquote(location))
    return None


def path_to_posix(path: pathlib.Path) -> str:
    return path.as_posix()


def prepend_path_prefix(path_text: str, path_prefix: str) -> str:
    if path_prefix == "":
        return path_text
    normalized_prefix: str = path_prefix.replace("\\", "/").rstrip("/")
    normalized_path: str = path_text.lstrip("/")
    return f"{normalized_prefix}/{normalized_path}"


def flacbox_path_for_location(location: str, mapping: PathMapping) -> str:
    local_path: pathlib.Path | None = rhythmdb_location_to_local_path(location)
    if mapping.path_mode == "uri":
        return location
    if mapping.path_mode == "absolute":
        if local_path is None:
            return location
        return path_to_posix(local_path)
    if mapping.path_mode != "relative":
        raise PlaylistToolError(f"Unsupported path mode: {mapping.path_mode}")
    if local_path is None:
        raise PlaylistToolError(f"Cannot convert non-file URI to a Flacbox relative path: {location}")
    if mapping.music_root is None:
        raise PlaylistToolError("--music-root is required when --path-mode relative")
    local_resolved: pathlib.Path = local_path.expanduser().resolve(strict=False)
    root_resolved: pathlib.Path = mapping.music_root.expanduser().resolve(strict=False)
    try:
        relative_path: pathlib.Path = local_resolved.relative_to(root_resolved)
    except ValueError as exc:
        if not mapping.allow_outside_root:
            raise PlaylistToolError(
                f"Track is outside --music-root. Track: {local_path}; music root: {mapping.music_root}"
            ) from exc
        return prepend_path_prefix(path_to_posix(local_resolved), mapping.path_prefix)
    return prepend_path_prefix(path_to_posix(relative_path), mapping.path_prefix)


def safe_playlist_filename(name: str, extension: str) -> str:
    cleaned: str = UNSAFE_FILENAME_RE.sub("_", name).strip().strip(".")
    if cleaned == "":
        cleaned = "playlist"
    return f"{cleaned}.{extension.lstrip('.')}"


def m3u_duration(song: Song | None) -> int:
    if song is None or song.duration is None:
        return -1
    return song.duration


def m3u_display_name(song: Song | None, location: str) -> str:
    if song is not None:
        artist: str = song.artist.strip()
        title: str = song.title.strip()
        if artist != "" and title != "":
            return f"{artist} - {title}"
        if title != "":
            return title
    local_path: pathlib.Path | None = rhythmdb_location_to_local_path(location)
    if local_path is not None:
        return local_path.name
    return location.rsplit("/", 1)[-1]


def make_m3u_text(
    playlist: PlaylistData,
    songs_by_location: dict[str, Song],
    mapping: PathMapping,
    include_extinf: bool,
    strict: bool,
) -> tuple[str, list[str]]:
    lines: list[str] = ["#EXTM3U"]
    missing_locations: list[str] = []
    for location in playlist.locations:
        song: Song | None = songs_by_location.get(location)
        if song is None:
            missing_locations.append(location)
            if strict:
                raise PlaylistToolError(
                    f"Playlist {playlist.name!r} has a location missing from rhythmdb.xml: {location}"
                )
        if include_extinf:
            lines.append(f"#EXTINF:{m3u_duration(song)}, {m3u_display_name(song, location)}")
        lines.append(flacbox_path_for_location(location, mapping))
    return "\n".join(lines) + "\n", missing_locations


def command_flacbox(args: argparse.Namespace) -> int:
    playlist_xml: pathlib.Path = args.playlist_xml.expanduser().resolve()
    db_xml: pathlib.Path = args.db_xml.expanduser().resolve()
    output_dir: pathlib.Path = args.output_dir.expanduser().resolve()
    selected_names: set[str] | None = set(args.playlist) if args.playlist else None
    music_root: pathlib.Path | None = None if args.music_root is None else args.music_root.expanduser()
    mapping: PathMapping = PathMapping(
        path_mode=args.path_mode,
        music_root=music_root,
        path_prefix=args.path_prefix,
        allow_outside_root=args.allow_outside_root,
    )
    if args.path_mode == "relative" and music_root is None:
        raise PlaylistToolError("--music-root is required when --path-mode relative")
    playlists: dict[str, PlaylistData] = read_playlists(playlist_xml, static_only=not args.include_non_static)
    songs_by_location: dict[str, Song] = read_rhythmdb(db_xml)
    if selected_names is not None:
        missing_playlist_names: set[str] = selected_names - set(playlists.keys())
        if missing_playlist_names:
            raise PlaylistToolError(f"Requested playlist(s) not found: {', '.join(sorted(missing_playlist_names))}")
    exported_count: int = 0
    total_missing: int = 0
    for playlist in playlists.values():
        if selected_names is not None and playlist.name not in selected_names:
            continue
        if args.skip_empty and len(playlist.locations) == 0:
            continue
        m3u_text: str
        missing_locations: list[str]
        m3u_text, missing_locations = make_m3u_text(
            playlist=playlist,
            songs_by_location=songs_by_location,
            mapping=mapping,
            include_extinf=not args.no_extinf,
            strict=args.strict,
        )
        total_missing += len(missing_locations)
        output_path: pathlib.Path = output_dir / safe_playlist_filename(playlist.name, args.format)
        write_text_file_atomic(output_path, m3u_text, overwrite=args.force)
        exported_count += 1
        print(f"Exported {playlist.name} -> {output_path} ({len(playlist.locations)} track(s))")
    print(f"Exported {exported_count} playlist file(s) to {output_dir}")
    if total_missing > 0:
        print(f"Warning: {total_missing} exported location(s) had no matching rhythmdb.xml metadata")
    return 0


def add_common_rhythmbox_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("playlist_xml", type=pathlib.Path, help="Path to Rhythmbox playlists.xml.")
    parser.add_argument("db_xml", type=pathlib.Path, help="Path to Rhythmbox rhythmdb.xml.")


def add_tag_discovery_args(parser: argparse.ArgumentParser, *, include_scope: bool) -> None:
    parser.add_argument("--source-playlist", default=DEFAULT_SOURCE_PLAYLIST, help=f"Source playlist. Default: {DEFAULT_SOURCE_PLAYLIST!r}.")
    if include_scope:
        parser.add_argument("--tag-scope", choices=("source", "database"), default="source", help="Discover tags from the source playlist or full database. Default: source.")
    parser.add_argument("--tag-separators", default=DEFAULT_TAG_SEPARATORS, help=f"Characters used to split genre strings. Default: {DEFAULT_TAG_SEPARATORS!r}. Use '' to disable splitting.")
    parser.add_argument("--tag-sort", choices=("name", "count"), default="name", help="Sort discovered tags alphabetically or by descending count. Default: name.")
    parser.add_argument("--case-sensitive-tags", action="store_true", help="Treat tags as case-sensitive.")
    parser.add_argument("--strict", action="store_true", help="Fail if a playlist location is missing from rhythmdb.xml.")


def add_tag_selection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tag", action="append", help="Select this genre tag non-interactively. Repeatable.")
    parser.add_argument("--tag-file", type=pathlib.Path, default=None, help="UTF-8 text file containing selected tags, one tag per line.")
    parser.add_argument("--all-tags", action="store_true", help="Select every discovered tag non-interactively.")
    parser.add_argument("--exclude-tag", action="append", help="Remove this tag from the selected set. Repeatable.")
    parser.add_argument("--no-interactive", action="store_true", help="Disable the checkbox prompt; fail if no tags were supplied.")
    parser.add_argument("--write-tag-file", type=pathlib.Path, default=None, help="Write final selected tags to this UTF-8 text file.")
    parser.add_argument("--force-tag-file", action="store_true", help="Overwrite --write-tag-file if it already exists.")
    parser.add_argument("--playlist-name-template", default=DEFAULT_PLAYLIST_NAME_TEMPLATE, help="Derived playlist name template. Supported placeholders: {tag}, {safe_tag}. Default: {tag}.")


def build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="rb_flacbox_cli.py",
        description="Maintain Rhythmbox tag-derived playlists and export them to Flacbox M3U/M3U8.",
    )
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser] = parser.add_subparsers(dest="command", required=True)

    tags_parser: argparse.ArgumentParser = subparsers.add_parser("tags", help="List discovered genre tags.")
    add_common_rhythmbox_args(tags_parser)
    add_tag_discovery_args(tags_parser, include_scope=True)
    tags_parser.add_argument("--format", choices=("table", "plain", "json"), default="table", help="Output format. Default: table.")
    tags_parser.add_argument("--playlist-name-template", default=DEFAULT_PLAYLIST_NAME_TEMPLATE, help="Playlist-name template used for status display.")
    tags_parser.set_defaults(func=command_tags)

    backup_parser: argparse.ArgumentParser = subparsers.add_parser("backup", help="Back up playlists.xml/rhythmdb.xml while excluding selected derived playlists.")
    add_common_rhythmbox_args(backup_parser)
    backup_parser.add_argument("backup_dir", type=pathlib.Path, help="Directory for backup files.")
    add_tag_discovery_args(backup_parser, include_scope=True)
    add_tag_selection_args(backup_parser)
    backup_parser.add_argument("--exclude-playlist", action="append", help="Additional static playlist name to exclude from the playlist backup. Repeatable.")
    backup_parser.add_argument("--date-stamp", default=None, help="Date/string used in backup filenames. Default: today's date as YYYY-MM-DD.")
    backup_parser.add_argument("--force", action="store_true", help="Overwrite existing backup files.")
    backup_parser.set_defaults(func=command_backup)

    derive_parser: argparse.ArgumentParser = subparsers.add_parser("derive", help="Create/replace tag-derived Rhythmbox static playlists.")
    add_common_rhythmbox_args(derive_parser)
    add_tag_discovery_args(derive_parser, include_scope=False)
    add_tag_selection_args(derive_parser)
    derive_parser.add_argument("--output", type=pathlib.Path, default=None, help="Output playlists.xml path. Default: overwrite input playlists.xml.")
    derive_parser.add_argument("--prune-unselected-tags", action="store_true", help="Also remove existing tag-named playlists for discovered but unselected tags.")
    derive_parser.add_argument("--allow-source-overwrite", action="store_true", help="Allow a selected tag to map to the source playlist name.")
    derive_parser.set_defaults(func=command_derive)

    flacbox_parser: argparse.ArgumentParser = subparsers.add_parser("flacbox", help="Export Rhythmbox playlists as Flacbox-compatible M3U/M3U8 files.")
    add_common_rhythmbox_args(flacbox_parser)
    flacbox_parser.add_argument("output_dir", type=pathlib.Path, help="Directory for exported playlist files.")
    flacbox_parser.add_argument("--format", choices=("m3u8", "m3u"), default="m3u8", help="Playlist file format. Default: m3u8.")
    flacbox_parser.add_argument("--path-mode", choices=("relative", "absolute", "uri"), default="relative", help="How to write media paths. Default: relative.")
    flacbox_parser.add_argument("--music-root", type=pathlib.Path, default=None, help="Local music root stripped from file URIs when --path-mode relative.")
    flacbox_parser.add_argument("--path-prefix", default="", help="Optional prefix prepended to exported media paths, e.g. Music or ../Music.")
    flacbox_parser.add_argument("--allow-outside-root", action="store_true", help="Allow absolute paths for files outside --music-root instead of failing.")
    flacbox_parser.add_argument("--playlist", action="append", help="Export only this playlist name. Repeatable. Default: export all static playlists.")
    flacbox_parser.add_argument("--include-non-static", action="store_true", help="Also export non-static playlist elements if they contain locations.")
    flacbox_parser.add_argument("--skip-empty", action="store_true", help="Do not export empty playlists.")
    flacbox_parser.add_argument("--no-extinf", action="store_true", help="Do not write #EXTINF metadata lines.")
    flacbox_parser.add_argument("--strict", action="store_true", help="Fail if any playlist location is missing from rhythmdb.xml.")
    flacbox_parser.add_argument("--force", action="store_true", help="Overwrite existing exported playlist files.")
    flacbox_parser.set_defaults(func=command_flacbox)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = build_parser()
    args: argparse.Namespace = parser.parse_args(argv)
    try:
        return args.func(args)
    except PlaylistToolError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
