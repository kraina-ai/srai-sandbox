"""
PBF File Downloader.

This module contains a downloader capable of downloading a PBF file from a free Protomaps service.
"""
import hashlib
import json
import warnings
from pathlib import Path
from time import sleep
from typing import Any, Dict, Hashable, Literal, Sequence, Tuple, Union

import geopandas as gpd
import requests
import shapely.wkt as wktlib
from shapely.geometry import Polygon, mapping
from shapely.geometry.base import BaseGeometry
from tqdm import tqdm

from srai.utils import download_file, flatten_geometry
from srai.utils.geometry import simplify_polygon_with_buffer
from srai.utils.openstreetmap_extracts import (
    find_smallest_containing_geofabrik_extracts_urls,
    find_smallest_containing_openstreetmap_fr_extracts_urls,
)

PbfSourceLiteral = Literal["protomaps", "geofabrik", "openstreetmap_fr"]


class PbfFileDownloader:
    """
    PbfFileDownloader.

    PBF(Protocolbuffer Binary Format)[1] file downloader is a downloader
    capable of downloading `*.osm.pbf` files with OSM data for a given area.

    This downloader uses free Protomaps[2] download service to extract a PBF
    file for a given region.

    References:
        1. https://wiki.openstreetmap.org/wiki/PBF_Format
        2. https://protomaps.com/
    """

    PROTOMAPS_API_START_URL = "https://app.protomaps.com/downloads/osm"
    PROTOMAPS_API_DOWNLOAD_URL = "https://app.protomaps.com/downloads/{}/download"

    _PBAR_FORMAT = "[{}] Downloading pbf file #{} ({})"

    def __init__(
        self, source: PbfSourceLiteral = "protomaps", download_directory: Union[str, Path] = "files"
    ) -> None:
        """
        Initialize PbfFileDownloader.

        Args:
            source (PbfSourceLiteral, optional): Source to use when downloading PBF files.
                Can be either `protomaps` or `geofabrik`.
                Defaults to "protomaps".
            download_directory (Union[str, Path], optional): Directory where to save
                 the downloaded `*.osm.pbf` files. Defaults to "files".
        """
        self.source = source
        self.download_directory = download_directory

    def download_pbf_files_for_regions_gdf(
        self, regions_gdf: gpd.GeoDataFrame
    ) -> Dict[Hashable, Sequence[Path]]:
        """
        Download PBF files for regions GeoDataFrame.

        Function will split each multipolygon into single polygons and download PBF files
        for each of them.

        Args:
            regions_gdf (gpd.GeoDataFrame): Region indexes and geometries.

        Returns:
            Dict[Hashable, Sequence[Path]]: List of Paths to downloaded PBF files per
                each region_id.
        """
        regions_mapping: Dict[Hashable, Sequence[Path]] = {}

        if self.source == "protomaps":
            regions_mapping = self._download_pbf_files_for_polygons_from_protomaps(regions_gdf)
        elif self.source in ["geofabrik", "openstreetmap_fr"]:
            regions_mapping = self._download_pbf_files_for_polygons_from_existing_extracts(
                regions_gdf
            )

        return regions_mapping

    def _download_pbf_files_for_polygons_from_existing_extracts(
        self, regions_gdf: gpd.GeoDataFrame
    ) -> Dict[Hashable, Sequence[Path]]:
        regions_mapping: Dict[Hashable, Sequence[Path]] = {}

        unary_union_geometry = regions_gdf.geometry.unary_union

        if self.source == "geofabrik":
            extracts = find_smallest_containing_geofabrik_extracts_urls(unary_union_geometry)
        elif self.source == "openstreetmap_fr":
            extracts = find_smallest_containing_openstreetmap_fr_extracts_urls(unary_union_geometry)

        for extract in extracts:
            pbf_file_path = Path(self.download_directory).resolve() / f"{extract.id}.osm.pbf"

            download_file(url=extract.url, fname=pbf_file_path.as_posix(), force_download=False)

            regions_mapping[extract.id] = [pbf_file_path]

        return regions_mapping

    def _download_pbf_files_for_polygons_from_protomaps(
        self, regions_gdf: gpd.GeoDataFrame
    ) -> Dict[Hashable, Sequence[Path]]:
        regions_mapping: Dict[Hashable, Sequence[Path]] = {}

        for region_id, row in regions_gdf.iterrows():
            polygons = flatten_geometry(row.geometry)
            regions_mapping[region_id] = [
                self._download_pbf_file_for_polygon_from_protomaps(
                    polygon, region_id, polygon_id + 1
                )
                for polygon_id, polygon in enumerate(polygons)
            ]

        return regions_mapping

    def _download_pbf_file_for_polygon_from_protomaps(
        self, polygon: Polygon, region_id: str = "OSM", polygon_id: int = 1
    ) -> Path:
        """
        Download PBF file for a single Polygon.

        Function will buffer polygon by 50 meters, simplify exterior boundary to be
        below 1000 points (which is a limit of Protomaps API) and close all holes within it.

        Boundary of the polygon will be sent to Protomaps service and an `*.osm.pbf` file
        will be downloaded with a hash based on WKT representation of the parsed polygon.
        If file exists, it won't be downloaded again.

        Args:
            polygon (Polygon): Polygon boundary of an area to be extracted.
            region_id (str, optional): Region name to be set in progress bar.
                Defaults to "OSM".
            polygon_id (int, optional): Polygon number to be set in progress bar.
                Defaults to 1.

        Returns:
            Path: Path to a downloaded `*.osm.pbf` file.
        """
        geometry_hash = self._get_geometry_hash(polygon)
        pbf_file_path = Path(self.download_directory).resolve() / f"{geometry_hash}.osm.pbf"

        if not pbf_file_path.exists():  # pragma: no cover
            boundary_polygon = simplify_polygon_with_buffer(polygon)
            geometry_geojson = mapping(boundary_polygon)

            session, start_extract_result = self._send_first_request(
                geometry_geojson, geometry_hash
            )

            try:
                extraction_uuid = start_extract_result["uuid"]
                status_check_url = start_extract_result["url"]
            except KeyError:
                warnings.warn(json.dumps(start_extract_result), stacklevel=2)
                raise

            with tqdm() as pbar:
                status_response: Dict[str, Any] = {}
                cells_total = 0
                nodes_total = 0
                elems_total = 0
                while not status_response.get("Complete", False):
                    sleep(0.5)
                    status_response = session.get(url=status_check_url).json()
                    cells_total = max(cells_total, status_response.get("CellsTotal", 0))
                    nodes_total = max(nodes_total, status_response.get("NodesTotal", 0))
                    elems_total = max(elems_total, status_response.get("ElemsTotal", 0))

                    cells_prog = status_response.get("CellsProg", None)
                    nodes_prog = status_response.get("NodesProg", None)
                    elems_prog = status_response.get("ElemsProg", None)

                    if cells_total > 0 and cells_prog is not None and cells_prog < cells_total:
                        pbar.set_description(
                            self._PBAR_FORMAT.format(region_id, polygon_id, "Cells")
                        )
                        pbar.total = cells_total + nodes_total + elems_total
                        pbar.n = cells_prog
                    elif nodes_total > 0 and nodes_prog is not None and nodes_prog < nodes_total:
                        pbar.set_description(
                            self._PBAR_FORMAT.format(region_id, polygon_id, "Nodes")
                        )
                        pbar.total = cells_total + nodes_total + elems_total
                        pbar.n = cells_total + nodes_prog
                    elif elems_total > 0 and elems_prog is not None and elems_prog < elems_total:
                        pbar.set_description(
                            self._PBAR_FORMAT.format(region_id, polygon_id, "Elements")
                        )
                        pbar.total = cells_total + nodes_total + elems_total
                        pbar.n = cells_total + nodes_total + elems_prog
                    else:
                        pbar.total = cells_total + nodes_total + elems_total
                        pbar.n = cells_total + nodes_total + elems_total

                    pbar.refresh()

            download_file(
                url=self.PROTOMAPS_API_DOWNLOAD_URL.format(extraction_uuid),
                fname=pbf_file_path.as_posix(),
            )

        return pbf_file_path

    def _send_first_request(
        self, geometry_geojson: Any, geometry_hash: str
    ) -> Tuple[requests.Session, Any]:
        successful_request = False
        while not successful_request:
            # TODO: remove print
            print("Next request")
            s = requests.Session()

            req = s.get(url=self.PROTOMAPS_API_START_URL)

            csrf_token = req.cookies["csrftoken"]
            headers = {
                "Referer": self.PROTOMAPS_API_START_URL,
                "Cookie": f"csrftoken={csrf_token}",
                "X-CSRFToken": csrf_token,
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "SRAI Python package (https://github.com/srai-lab/srai)",
            }
            request_payload = {
                "region": {"type": "geojson", "data": geometry_geojson},
                "name": geometry_hash,
            }

            start_extract_request = s.post(
                url=self.PROTOMAPS_API_START_URL,
                json=request_payload,
                headers=headers,
                cookies=dict(csrftoken=csrf_token),
            )
            start_extract_request.raise_for_status()

            start_extract_result = start_extract_request.json()
            errors = start_extract_result.get("errors")
            if errors and "rate limited" in errors:
                # TODO: remove print
                print(start_extract_result)
                warnings.warn("Rate limited. Waiting 60 seconds.", stacklevel=2)
                sleep(300)
            else:
                successful_request = True

        return s, start_extract_result

    def _get_geometry_hash(self, geometry: BaseGeometry) -> str:
        """Generate SHA256 hash based on WKT representation of the polygon."""
        wkt_string = wktlib.dumps(geometry)
        h = hashlib.new("sha256")
        h.update(wkt_string.encode())
        return h.hexdigest()
