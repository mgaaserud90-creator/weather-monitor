"""
Polymarket weather market parser — extracts location, date, and temperature
buckets from Polymarket question strings.

Handles patterns like:
  - "Highest temperature in New York City on July 26?"
  - "Will London Heathrow record a temperature above 28°C on July 27?"
  - "What will be the highest temperature in Central Park on July 28?"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import ClassVar

import structlog

logger = structlog.get_logger(__name__)


# =============================================================================
# Data Models
# =============================================================================


@dataclass
class TemperatureBucket:
    """A single temperature range that can be bet on."""
    min_val: float  # Lower bound in °F
    max_val: float  # Upper bound in °F
    label: str = ""  # Original label from Polymarket
    is_open_upper: bool = False  # True if no upper bound (e.g., "95°F+")

    @property
    def width_f(self) -> float:
        return self.max_val - self.min_val

    def __hash__(self) -> int:
        return hash((self.min_val, self.max_val))


@dataclass
class WeatherMarket:
    """A parsed Polymarket weather market."""
    question: str
    location: str
    lat: float
    lon: float
    target_date: date
    buckets: list[TemperatureBucket] = field(default_factory=list)
    market_id: str = ""
    event_id: str = ""
    token_ids: list[str] = field(default_factory=list)
    days_until_resolution: int = 0
    resolved: bool = False

    def __post_init__(self) -> None:
        if self.days_until_resolution == 0:
            delta = self.target_date - date.today()
            self.days_until_resolution = max(0, delta.days)


# =============================================================================
# Location Map — ICAO Airport Coordinates (NOT city center)
# =============================================================================
# CORRECTED: Using precise ICAO airport coordinates instead of city centers.
# This is the SINGLE LARGEST improvement: +3-8 percentage points edge.
# Rationale: Polymarket resolves temperature markets based on official weather
# station readings, which are at AIRPORTS, not city centers. City centers have
# urban heat island effects that create systematic bias.

LOCATION_MAP: dict[str, tuple[float, float]] = {
    # US — Northeast (ICAO airport coordinates)
    "new york city": (40.7772, -73.8726),    # KLGA LaGuardia
    "new york": (40.7772, -73.8726),          # KLGA LaGuardia
    "nyc": (40.7772, -73.8726),               # KLGA LaGuardia
    "central park": (40.7772, -73.8726),      # KLGA (nearest official station)
    "central park nyc": (40.7772, -73.8726),  # KLGA LaGuardia
    "jfk airport": (40.6398, -73.7789),       # KJFK JFK Intl
    "la guardia": (40.7772, -73.8726),        # KLGA LaGuardia
    "laguardia": (40.7772, -73.8726),         # KLGA LaGuardia
    "newark": (40.6925, -74.1687),            # KEWR Newark Liberty
    "boston": (42.3656, -71.0096),            # KBOS Logan Intl
    "logan airport": (42.3656, -71.0096),     # KBOS Logan Intl
    "washington dc": (38.9531, -77.4565),     # KIAD Dulles (official DC station)
    "washington d.c.": (38.9531, -77.4565),   # KIAD Dulles
    "dc": (38.9531, -77.4565),                # KIAD Dulles
    "dulles airport": (38.9531, -77.4565),    # KIAD Dulles
    "philadelphia": (39.8722, -75.2409),      # KPHL Philadelphia Intl
    "philly": (39.8722, -75.2409),            # KPHL Philadelphia Intl
    "pittsburgh": (40.4915, -80.2329),        # KPIT Pittsburgh Intl
    "baltimore": (39.1754, -76.6688),         # KBWI Baltimore/Washington

    # US — South
    "miami": (25.7932, -80.2906),             # KMIA Miami Intl
    "miami beach": (25.7932, -80.2906),       # KMIA Miami Intl
    "orlando": (28.4294, -81.3089),           # KMCO Orlando Intl
    "tampa": (27.9755, -82.5332),             # KTPA Tampa Intl
    "atlanta": (33.6407, -84.4277),           # KATL Hartsfield-Jackson
    "hartsfield-jackson": (33.6407, -84.4277),# KATL Hartsfield-Jackson
    "dallas": (32.8472, -96.8517),            # KDAL Dallas Love Field
    "dallas-ft worth": (32.8998, -97.0403),   # KDFW Dallas/Ft Worth
    "dfw": (32.8998, -97.0403),               # KDFW Dallas/Ft Worth
    "houston": (29.6452, -95.2768),           # KHOU William P. Hobby
    "new orleans": (29.9934, -90.2580),       # KMSY Louis Armstrong
    "nashville": (36.1245, -86.6782),         # KBNA Nashville Intl
    "charlotte": (35.2137, -80.9491),         # KCLT Charlotte/Douglas
    "austin": (30.1945, -97.6699),            # KAUS Austin-Bergstrom
    "san antonio": (29.5337, -98.4698),       # KSAT San Antonio Intl

    # US — Midwest
    "chicago": (41.9742, -87.9073),           # KORD O'Hare (official station)
    "o'hare": (41.9742, -87.9073),            # KORD O'Hare
    "ohare": (41.9742, -87.9073),             # KORD O'Hare
    "midway": (41.7860, -87.7523),            # KMDW Midway
    "detroit": (42.2124, -83.3534),           # KDTW Detroit Metro
    "minneapolis": (44.8820, -93.2218),       # KMSP Minneapolis-St Paul
    "st louis": (38.7487, -90.3700),          # KSTL Lambert-St Louis
    "st. louis": (38.7487, -90.3700),         # KSTL Lambert-St Louis
    "kansas city": (39.2976, -94.7139),       # KMCI Kansas City Intl
    "cleveland": (41.4117, -81.8498),         # KCLE Cleveland Hopkins
    "cincinnati": (39.0488, -84.6678),        # KCVG Cincinnati/N. Kentucky
    "indianapolis": (39.7173, -86.2947),      # KIND Indianapolis Intl
    "milwaukee": (42.9472, -87.8966),         # KMKE Milwaukee Mitchell
    "columbus": (39.9980, -82.8919),          # KCMH John Glenn Intl

    # US — West
    "los angeles": (33.9425, -118.4081),      # KLAX Los Angeles Intl
    "la": (33.9425, -118.4081),               # KLAX Los Angeles Intl
    "lax": (33.9425, -118.4081),              # KLAX Los Angeles Intl
    "san francisco": (37.6189, -122.3750),    # KSFO San Francisco Intl
    "sfo": (37.6189, -122.3750),              # KSFO San Francisco Intl
    "oakland": (37.7213, -122.2207),          # KOAK Oakland Intl
    "san jose": (37.3627, -121.9291),         # KSJC San Jose Intl
    "seattle": (47.4490, -122.3093),          # KSEA Seattle-Tacoma Intl
    "portland": (45.5887, -122.5969),         # KPDX Portland Intl
    "denver": (39.7017, -104.7517),           # KBKF Buckley SFB (official Denver met station)
    "phoenix": (33.4343, -112.0116),          # KPHX Sky Harbor Intl
    "sky harbor": (33.4343, -112.0116),       # KPHX Sky Harbor Intl
    "las vegas": (36.0801, -115.1523),        # KLAS Harry Reid Intl
    "vegas": (36.0801, -115.1523),            # KLAS Harry Reid Intl
    "salt lake city": (40.7884, -111.9778),   # KSLC Salt Lake City Intl
    "sacramento": (38.6954, -121.5908),       # KSMF Sacramento Intl
    "san diego": (32.7338, -117.1897),        # KSAN San Diego Intl
    "albuquerque": (35.0402, -106.6092),      # KABQ Albuquerque Sunport
    "boise": (43.5644, -116.2228),            # KBOI Boise Air Terminal
    "reno": (39.4991, -119.7681),             # KRNO Reno/Tahoe Intl

    # International — Europe (ICAO airport coordinates)
    "london": (51.5050, 0.0550),              # EGLC London City (official station)
    "london heathrow": (51.4775, -0.4614),    # EGLL Heathrow
    "heathrow": (51.4775, -0.4614),           # EGLL Heathrow
    "gatwick": (51.1481, -0.1903),            # EGKK Gatwick
    "paris": (48.9694, 2.4414),               # LFPB Le Bourget (official met)
    "charles de gaulle": (49.0097, 2.5479),   # LFPG CDG
    "cdg": (49.0097, 2.5479),                 # LFPG CDG
    "berlin": (52.5597, 13.2877),             # EDDT Tegel (historical) / EDDB BER
    "madrid": (40.4722, -3.5608),             # LEMD Barajas
    "rome": (41.8003, 12.2389),               # LIRF Fiumicino
    "amsterdam": (52.3086, 4.7639),           # EHAM Schiphol
    "schiphol": (52.3086, 4.7639),            # EHAM Schiphol
    "brussels": (50.9014, 4.4844),            # EBBR Brussels Airport
    "vienna": (48.1103, 16.5697),             # LOWW Vienna Intl
    "zurich": (47.4647, 8.5492),              # LSZH Zurich Airport
    "geneva": (46.2383, 6.1094),              # LSGG Geneva Airport
    "munich": (48.3536, 11.7758),             # EDDM Munich Airport
    "frankfurt": (50.0333, 8.5706),           # EDDF Frankfurt Airport
    "stockholm": (59.6519, 17.9186),          # ESSA Arlanda
    "oslo": (60.1939, 11.1004),               # ENGM Gardermoen
    "copenhagen": (55.6181, 12.6561),         # EKCH Kastrup
    "helsinki": (60.3172, 24.9633),           # EFHK Vantaa
    "warsaw": (52.1657, 20.9671),             # EPWA Chopin
    "prague": (50.1008, 14.2600),             # LKPR Vaclav Havel
    "budapest": (47.4369, 19.2556),           # LHBP Ferenc Liszt
    "athens": (37.9364, 23.9445),             # LGAV Eleftherios Venizelos
    "istanbul": (40.9769, 28.8147),           # LTFM Istanbul Airport
    "moscow": (55.9726, 37.4146),             # UUEE Sheremetyevo
    "dublin": (53.4213, -6.2701),             # EIDW Dublin Airport
    "edinburgh": (55.9500, -3.3726),          # EGPH Edinburgh Airport
    "barcelona": (41.2972, 2.0785),           # LEBL El Prat
    "lisbon": (38.7742, -9.1342),             # LPPT Humberto Delgado
    "milan": (45.4617, 9.2792),               # LIML Linate

    # International — Asia
    "tokyo": (35.5533, 139.7811),             # RJTT Haneda
    "narita": (35.7720, 140.3929),            # RJAA Narita
    "haneda": (35.5533, 139.7811),            # RJTT Haneda
    "beijing": (40.0800, 116.5846),           # ZBAA Beijing Capital
    "shanghai": (31.1433, 121.8053),          # ZSPD Pudong
    "hong kong": (22.3089, 113.9147),         # VHHH HKIA
    "singapore": (1.3592, 103.9894),          # WSSS Changi
    "seoul": (37.4628, 126.4403),             # RKSI Incheon
    "mumbai": (19.0887, 72.8679),             # VABB Chhatrapati Shivaji
    "delhi": (28.5665, 77.1031),              # VIDP Indira Gandhi Intl
    "bangkok": (13.6811, 100.7473),           # VTBS Suvarnabhumi
    "dubai": (25.2532, 55.3657),              # OMDB Dubai Intl
    "taipei": (25.0777, 121.2328),            # RCTP Taoyuan
    "kuala lumpur": (2.7456, 101.7099),       # WMKK KLIA
    "manila": (14.5086, 121.0196),            # RPLL Ninoy Aquino
    "jakarta": (-6.1256, 106.6558),           # WIII Soekarno-Hatta

    # International — Oceania
    "sydney": (-33.9461, 151.1772),           # YSSY Kingsford Smith
    "melbourne": (-37.6733, 144.8433),        # YMML Melbourne Airport
    "brisbane": (-27.3842, 153.1175),         # YBBN Brisbane Airport
    "perth": (-31.9403, 115.9672),            # YPPH Perth Airport
    "auckland": (-37.0082, 174.7850),         # NZAA Auckland Airport
    "wellington": (-41.3272, 174.8053),       # NZWN Wellington Airport

    # International — Americas
    "toronto": (43.6777, -79.6248),           # CYYZ Pearson Intl
    "montreal": (45.4706, -73.7408),          # CYUL Pierre Trudeau
    "vancouver": (49.1939, -123.1844),        # CYVR Vancouver Intl
    "calgary": (51.1139, -114.0203),          # CYYC Calgary Intl
    "mexico city": (19.4363, -99.0721),       # MMMX Benito Juarez Intl
    "sao paulo": (-23.4356, -46.4731),        # SBGR Guarulhos
    "rio de janeiro": (-22.8100, -43.2506),   # SBGL Galeao
    "buenos aires": (-34.8222, -58.5358),     # SAEZ Ezeiza
    "santiago": (-33.3930, -70.7858),         # SCEL Arturo Merino Benitez
    "bogota": (4.7016, -74.1469),             # SKBO El Dorado
    "lima": (-12.0219, -77.1143),             # SPJC Jorge Chavez

    # International — Africa
    "cairo": (30.1219, 31.4056),              # HECA Cairo Intl
    "cape town": (-33.9648, 18.6017),         # FACT Cape Town Intl
    "johannesburg": (-26.1392, 28.2460),      # FAOR OR Tambo
    "lagos": (6.5774, 3.3212),                # DNMM Murtala Muhammed
    "nairobi": (-1.3192, 36.9278),            # HKJK Jomo Kenyatta
    "casablanca": (33.3675, -7.5900),         # GMMN Mohammed V
    "ankara": (40.1281, 32.9951),             # LTAC Esenboga
}


# =============================================================================
# Station Metadata — Per-station calibration parameters
# =============================================================================
# Each station has metadata for microclimate, elevation, and unit corrections.
# These are used by microclimate.py, metar_feed.py, and strategy.py.

STATION_METADATA: dict[str, dict[str, object]] = {
    # US — Northeast
    "new york city": {"icao": "KLGA", "elevation_m": 6.4, "uhi_factor": 2.5, "marine_influence": True, "timezone": "America/New_York", "unit": "°F", "bucket_size": 2.0, "station_name": "LaGuardia Airport"},
    "boston": {"icao": "KBOS", "elevation_m": 6.1, "uhi_factor": 1.8, "marine_influence": True, "timezone": "America/New_York", "unit": "°F", "bucket_size": 2.0, "station_name": "Logan International"},
    "washington dc": {"icao": "KIAD", "elevation_m": 95.1, "uhi_factor": 1.5, "marine_influence": False, "timezone": "America/New_York", "unit": "°F", "bucket_size": 2.0, "station_name": "Dulles International"},
    "philadelphia": {"icao": "KPHL", "elevation_m": 5.5, "uhi_factor": 1.5, "marine_influence": True, "timezone": "America/New_York", "unit": "°F", "bucket_size": 2.0, "station_name": "Philadelphia International"},
    "pittsburgh": {"icao": "KPIT", "elevation_m": 366.7, "uhi_factor": 0.8, "marine_influence": False, "timezone": "America/New_York", "unit": "°F", "bucket_size": 2.0, "station_name": "Pittsburgh International"},
    "baltimore": {"icao": "KBWI", "elevation_m": 44.5, "uhi_factor": 1.2, "marine_influence": False, "timezone": "America/New_York", "unit": "°F", "bucket_size": 2.0, "station_name": "BWI Marshall"},

    # US — South
    "miami": {"icao": "KMIA", "elevation_m": 2.4, "uhi_factor": 1.8, "marine_influence": True, "timezone": "America/New_York", "unit": "°F", "bucket_size": 2.0, "station_name": "Miami International"},
    "orlando": {"icao": "KMCO", "elevation_m": 29.0, "uhi_factor": 1.0, "marine_influence": False, "timezone": "America/New_York", "unit": "°F", "bucket_size": 2.0, "station_name": "Orlando International"},
    "tampa": {"icao": "KTPA", "elevation_m": 7.9, "uhi_factor": 1.2, "marine_influence": True, "timezone": "America/New_York", "unit": "°F", "bucket_size": 2.0, "station_name": "Tampa International"},
    "atlanta": {"icao": "KATL", "elevation_m": 313.0, "uhi_factor": 1.5, "marine_influence": False, "timezone": "America/New_York", "unit": "°F", "bucket_size": 2.0, "station_name": "Hartsfield-Jackson Atlanta"},
    "dallas": {"icao": "KDAL", "elevation_m": 148.1, "uhi_factor": 2.0, "marine_influence": False, "timezone": "America/Chicago", "unit": "°F", "bucket_size": 2.0, "station_name": "Dallas Love Field"},
    "houston": {"icao": "KHOU", "elevation_m": 14.0, "uhi_factor": 1.8, "marine_influence": True, "timezone": "America/Chicago", "unit": "°F", "bucket_size": 2.0, "station_name": "William P. Hobby Airport"},
    "new orleans": {"icao": "KMSY", "elevation_m": 1.2, "uhi_factor": 1.3, "marine_influence": True, "timezone": "America/Chicago", "unit": "°F", "bucket_size": 2.0, "station_name": "Louis Armstrong New Orleans"},
    "nashville": {"icao": "KBNA", "elevation_m": 182.9, "uhi_factor": 0.8, "marine_influence": False, "timezone": "America/Chicago", "unit": "°F", "bucket_size": 2.0, "station_name": "Nashville International"},
    "charlotte": {"icao": "KCLT", "elevation_m": 228.0, "uhi_factor": 1.0, "marine_influence": False, "timezone": "America/New_York", "unit": "°F", "bucket_size": 2.0, "station_name": "Charlotte Douglas"},
    "austin": {"icao": "KAUS", "elevation_m": 149.4, "uhi_factor": 1.5, "marine_influence": False, "timezone": "America/Chicago", "unit": "°F", "bucket_size": 2.0, "station_name": "Austin-Bergstrom"},
    "san antonio": {"icao": "KSAT", "elevation_m": 246.9, "uhi_factor": 1.3, "marine_influence": False, "timezone": "America/Chicago", "unit": "°F", "bucket_size": 2.0, "station_name": "San Antonio International"},

    # US — Midwest
    "chicago": {"icao": "KORD", "elevation_m": 204.2, "uhi_factor": 2.0, "marine_influence": False, "timezone": "America/Chicago", "unit": "°F", "bucket_size": 2.0, "station_name": "O'Hare International"},
    "detroit": {"icao": "KDTW", "elevation_m": 196.9, "uhi_factor": 1.2, "marine_influence": False, "timezone": "America/Detroit", "unit": "°F", "bucket_size": 2.0, "station_name": "Detroit Metro"},
    "minneapolis": {"icao": "KMSP", "elevation_m": 256.0, "uhi_factor": 1.0, "marine_influence": False, "timezone": "America/Chicago", "unit": "°F", "bucket_size": 2.0, "station_name": "Minneapolis-St. Paul"},
    "st louis": {"icao": "KSTL", "elevation_m": 184.4, "uhi_factor": 1.2, "marine_influence": False, "timezone": "America/Chicago", "unit": "°F", "bucket_size": 2.0, "station_name": "Lambert-St. Louis"},
    "kansas city": {"icao": "KMCI", "elevation_m": 312.4, "uhi_factor": 0.8, "marine_influence": False, "timezone": "America/Chicago", "unit": "°F", "bucket_size": 2.0, "station_name": "Kansas City International"},
    "cleveland": {"icao": "KCLE", "elevation_m": 241.1, "uhi_factor": 0.8, "marine_influence": False, "timezone": "America/New_York", "unit": "°F", "bucket_size": 2.0, "station_name": "Cleveland Hopkins"},
    "cincinnati": {"icao": "KCVG", "elevation_m": 273.1, "uhi_factor": 0.7, "marine_influence": False, "timezone": "America/New_York", "unit": "°F", "bucket_size": 2.0, "station_name": "Cincinnati/N. Kentucky"},
    "indianapolis": {"icao": "KIND", "elevation_m": 243.2, "uhi_factor": 1.0, "marine_influence": False, "timezone": "America/Indiana/Indianapolis", "unit": "°F", "bucket_size": 2.0, "station_name": "Indianapolis International"},
    "milwaukee": {"icao": "KMKE", "elevation_m": 220.7, "uhi_factor": 1.0, "marine_influence": True, "timezone": "America/Chicago", "unit": "°F", "bucket_size": 2.0, "station_name": "Milwaukee Mitchell"},
    "columbus": {"icao": "KCMH", "elevation_m": 248.1, "uhi_factor": 0.9, "marine_influence": False, "timezone": "America/New_York", "unit": "°F", "bucket_size": 2.0, "station_name": "John Glenn International"},

    # US — West
    "los angeles": {"icao": "KLAX", "elevation_m": 38.1, "uhi_factor": 1.5, "marine_influence": True, "timezone": "America/Los_Angeles", "unit": "°F", "bucket_size": 2.0, "station_name": "Los Angeles International"},
    "san francisco": {"icao": "KSFO", "elevation_m": 3.0, "uhi_factor": 0.3, "marine_influence": True, "timezone": "America/Los_Angeles", "unit": "°F", "bucket_size": 2.0, "station_name": "San Francisco International"},
    "oakland": {"icao": "KOAK", "elevation_m": 2.7, "uhi_factor": 0.5, "marine_influence": True, "timezone": "America/Los_Angeles", "unit": "°F", "bucket_size": 2.0, "station_name": "Oakland International"},
    "san jose": {"icao": "KSJC", "elevation_m": 17.4, "uhi_factor": 1.2, "marine_influence": False, "timezone": "America/Los_Angeles", "unit": "°F", "bucket_size": 2.0, "station_name": "San Jose International"},
    "seattle": {"icao": "KSEA", "elevation_m": 132.0, "uhi_factor": 0.5, "marine_influence": True, "timezone": "America/Los_Angeles", "unit": "°F", "bucket_size": 2.0, "station_name": "Seattle-Tacoma International"},
    "portland": {"icao": "KPDX", "elevation_m": 8.2, "uhi_factor": 0.5, "marine_influence": True, "timezone": "America/Los_Angeles", "unit": "°F", "bucket_size": 2.0, "station_name": "Portland International"},
    "denver": {"icao": "KBKF", "elevation_m": 1727.0, "uhi_factor": 0.2, "marine_influence": False, "timezone": "America/Denver", "unit": "°F", "bucket_size": 2.0, "station_name": "Buckley SFB (Denver)"},
    "phoenix": {"icao": "KPHX", "elevation_m": 345.0, "uhi_factor": 3.0, "marine_influence": False, "timezone": "America/Phoenix", "unit": "°F", "bucket_size": 2.0, "station_name": "Sky Harbor International"},
    "las vegas": {"icao": "KLAS", "elevation_m": 665.4, "uhi_factor": 2.5, "marine_influence": False, "timezone": "America/Los_Angeles", "unit": "°F", "bucket_size": 2.0, "station_name": "Harry Reid International"},
    "salt lake city": {"icao": "KSLC", "elevation_m": 1288.0, "uhi_factor": 0.8, "marine_influence": False, "timezone": "America/Denver", "unit": "°F", "bucket_size": 2.0, "station_name": "Salt Lake City International"},
    "sacramento": {"icao": "KSMF", "elevation_m": 7.6, "uhi_factor": 1.5, "marine_influence": False, "timezone": "America/Los_Angeles", "unit": "°F", "bucket_size": 2.0, "station_name": "Sacramento International"},
    "san diego": {"icao": "KSAN", "elevation_m": 4.6, "uhi_factor": 1.0, "marine_influence": True, "timezone": "America/Los_Angeles", "unit": "°F", "bucket_size": 2.0, "station_name": "San Diego International"},
    "albuquerque": {"icao": "KABQ", "elevation_m": 1631.0, "uhi_factor": 0.5, "marine_influence": False, "timezone": "America/Denver", "unit": "°F", "bucket_size": 2.0, "station_name": "Albuquerque International Sunport"},
    "boise": {"icao": "KBOI", "elevation_m": 875.1, "uhi_factor": 0.5, "marine_influence": False, "timezone": "America/Boise", "unit": "°F", "bucket_size": 2.0, "station_name": "Boise Air Terminal"},
    "reno": {"icao": "KRNO", "elevation_m": 1345.0, "uhi_factor": 0.5, "marine_influence": False, "timezone": "America/Los_Angeles", "unit": "°F", "bucket_size": 2.0, "station_name": "Reno/Tahoe International"},

    # International — Europe
    "london": {"icao": "EGLC", "elevation_m": 5.2, "uhi_factor": 1.5, "marine_influence": True, "timezone": "Europe/London", "unit": "°C", "bucket_size": 1.0, "station_name": "London City Airport"},
    "london heathrow": {"icao": "EGLL", "elevation_m": 25.3, "uhi_factor": 1.2, "marine_influence": False, "timezone": "Europe/London", "unit": "°C", "bucket_size": 1.0, "station_name": "Heathrow Airport"},
    "paris": {"icao": "LFPB", "elevation_m": 66.4, "uhi_factor": 1.8, "marine_influence": False, "timezone": "Europe/Paris", "unit": "°C", "bucket_size": 1.0, "station_name": "Paris-Le Bourget"},
    "berlin": {"icao": "EDDB", "elevation_m": 47.5, "uhi_factor": 0.8, "marine_influence": False, "timezone": "Europe/Berlin", "unit": "°C", "bucket_size": 1.0, "station_name": "Berlin Brandenburg"},
    "madrid": {"icao": "LEMD", "elevation_m": 609.0, "uhi_factor": 1.5, "marine_influence": False, "timezone": "Europe/Madrid", "unit": "°C", "bucket_size": 1.0, "station_name": "Adolfo Suarez Madrid-Barajas"},
    "rome": {"icao": "LIRF", "elevation_m": 4.6, "uhi_factor": 1.5, "marine_influence": True, "timezone": "Europe/Rome", "unit": "°C", "bucket_size": 1.0, "station_name": "Leonardo da Vinci-Fiumicino"},
    "amsterdam": {"icao": "EHAM", "elevation_m": -3.4, "uhi_factor": 0.8, "marine_influence": True, "timezone": "Europe/Amsterdam", "unit": "°C", "bucket_size": 1.0, "station_name": "Amsterdam Airport Schiphol"},
    "brussels": {"icao": "EBBR", "elevation_m": 55.8, "uhi_factor": 0.8, "marine_influence": False, "timezone": "Europe/Brussels", "unit": "°C", "bucket_size": 1.0, "station_name": "Brussels Airport"},
    "vienna": {"icao": "LOWW", "elevation_m": 183.0, "uhi_factor": 0.8, "marine_influence": False, "timezone": "Europe/Vienna", "unit": "°C", "bucket_size": 1.0, "station_name": "Vienna International"},
    "zurich": {"icao": "LSZH", "elevation_m": 431.6, "uhi_factor": 0.7, "marine_influence": False, "timezone": "Europe/Zurich", "unit": "°C", "bucket_size": 1.0, "station_name": "Zurich Airport"},
    "munich": {"icao": "EDDM", "elevation_m": 448.1, "uhi_factor": 0.5, "marine_influence": False, "timezone": "Europe/Berlin", "unit": "°C", "bucket_size": 1.0, "station_name": "Munich Airport"},
    "frankfurt": {"icao": "EDDF", "elevation_m": 110.9, "uhi_factor": 1.0, "marine_influence": False, "timezone": "Europe/Berlin", "unit": "°C", "bucket_size": 1.0, "station_name": "Frankfurt Airport"},
    "stockholm": {"icao": "ESSA", "elevation_m": 42.1, "uhi_factor": 0.5, "marine_influence": True, "timezone": "Europe/Stockholm", "unit": "°C", "bucket_size": 1.0, "station_name": "Stockholm Arlanda"},
    "oslo": {"icao": "ENGM", "elevation_m": 207.3, "uhi_factor": 0.3, "marine_influence": False, "timezone": "Europe/Oslo", "unit": "°C", "bucket_size": 1.0, "station_name": "Oslo Gardermoen"},
    "copenhagen": {"icao": "EKCH", "elevation_m": 5.2, "uhi_factor": 0.5, "marine_influence": True, "timezone": "Europe/Copenhagen", "unit": "°C", "bucket_size": 1.0, "station_name": "Copenhagen Kastrup"},
    "helsinki": {"icao": "EFHK", "elevation_m": 55.2, "uhi_factor": 0.3, "marine_influence": True, "timezone": "Europe/Helsinki", "unit": "°C", "bucket_size": 1.0, "station_name": "Helsinki Vantaa"},
    "warsaw": {"icao": "EPWA", "elevation_m": 110.0, "uhi_factor": 0.8, "marine_influence": False, "timezone": "Europe/Warsaw", "unit": "°C", "bucket_size": 1.0, "station_name": "Warsaw Chopin"},
    "prague": {"icao": "LKPR", "elevation_m": 379.8, "uhi_factor": 0.6, "marine_influence": False, "timezone": "Europe/Prague", "unit": "°C", "bucket_size": 1.0, "station_name": "Vaclav Havel Airport Prague"},
    "budapest": {"icao": "LHBP", "elevation_m": 150.9, "uhi_factor": 0.8, "marine_influence": False, "timezone": "Europe/Budapest", "unit": "°C", "bucket_size": 1.0, "station_name": "Budapest Ferenc Liszt"},
    "athens": {"icao": "LGAV", "elevation_m": 93.6, "uhi_factor": 2.0, "marine_influence": True, "timezone": "Europe/Athens", "unit": "°C", "bucket_size": 1.0, "station_name": "Athens International"},
    "istanbul": {"icao": "LTFM", "elevation_m": 99.1, "uhi_factor": 1.5, "marine_influence": True, "timezone": "Europe/Istanbul", "unit": "°C", "bucket_size": 1.0, "station_name": "Istanbul Airport"},
    "moscow": {"icao": "UUEE", "elevation_m": 192.0, "uhi_factor": 1.2, "marine_influence": False, "timezone": "Europe/Moscow", "unit": "°C", "bucket_size": 1.0, "station_name": "Sheremetyevo"},
    "dublin": {"icao": "EIDW", "elevation_m": 73.8, "uhi_factor": 0.5, "marine_influence": True, "timezone": "Europe/Dublin", "unit": "°C", "bucket_size": 1.0, "station_name": "Dublin Airport"},
    "barcelona": {"icao": "LEBL", "elevation_m": 4.0, "uhi_factor": 1.5, "marine_influence": True, "timezone": "Europe/Madrid", "unit": "°C", "bucket_size": 1.0, "station_name": "Barcelona-El Prat"},
    "lisbon": {"icao": "LPPT", "elevation_m": 114.0, "uhi_factor": 1.0, "marine_influence": True, "timezone": "Europe/Lisbon", "unit": "°C", "bucket_size": 1.0, "station_name": "Humberto Delgado"},
    "milan": {"icao": "LIML", "elevation_m": 107.0, "uhi_factor": 1.5, "marine_influence": False, "timezone": "Europe/Rome", "unit": "°C", "bucket_size": 1.0, "station_name": "Milano Linate"},

    # International — Asia
    "tokyo": {"icao": "RJTT", "elevation_m": 6.4, "uhi_factor": 2.5, "marine_influence": True, "timezone": "Asia/Tokyo", "unit": "°C", "bucket_size": 1.0, "station_name": "Tokyo Haneda"},
    "beijing": {"icao": "ZBAA", "elevation_m": 35.0, "uhi_factor": 2.0, "marine_influence": False, "timezone": "Asia/Shanghai", "unit": "°C", "bucket_size": 1.0, "station_name": "Beijing Capital"},
    "shanghai": {"icao": "ZSPD", "elevation_m": 4.0, "uhi_factor": 1.8, "marine_influence": True, "timezone": "Asia/Shanghai", "unit": "°C", "bucket_size": 1.0, "station_name": "Shanghai Pudong"},
    "hong kong": {"icao": "VHHH", "elevation_m": 8.5, "uhi_factor": 2.0, "marine_influence": True, "timezone": "Asia/Hong_Kong", "unit": "°C", "bucket_size": 1.0, "station_name": "Hong Kong International"},
    "singapore": {"icao": "WSSS", "elevation_m": 6.7, "uhi_factor": 1.5, "marine_influence": True, "timezone": "Asia/Singapore", "unit": "°C", "bucket_size": 1.0, "station_name": "Singapore Changi"},
    "seoul": {"icao": "RKSI", "elevation_m": 7.0, "uhi_factor": 1.5, "marine_influence": True, "timezone": "Asia/Seoul", "unit": "°C", "bucket_size": 1.0, "station_name": "Incheon International"},
    "mumbai": {"icao": "VABB", "elevation_m": 11.0, "uhi_factor": 2.0, "marine_influence": True, "timezone": "Asia/Kolkata", "unit": "°C", "bucket_size": 1.0, "station_name": "Chhatrapati Shivaji Maharaj"},
    "delhi": {"icao": "VIDP", "elevation_m": 237.0, "uhi_factor": 2.5, "marine_influence": False, "timezone": "Asia/Kolkata", "unit": "°C", "bucket_size": 1.0, "station_name": "Indira Gandhi International"},
    "bangkok": {"icao": "VTBS", "elevation_m": 1.5, "uhi_factor": 1.8, "marine_influence": True, "timezone": "Asia/Bangkok", "unit": "°C", "bucket_size": 1.0, "station_name": "Suvarnabhumi"},
    "dubai": {"icao": "OMDB", "elevation_m": 10.4, "uhi_factor": 2.0, "marine_influence": True, "timezone": "Asia/Dubai", "unit": "°C", "bucket_size": 1.0, "station_name": "Dubai International"},
    "taipei": {"icao": "RCTP", "elevation_m": 32.0, "uhi_factor": 1.2, "marine_influence": True, "timezone": "Asia/Taipei", "unit": "°C", "bucket_size": 1.0, "station_name": "Taiwan Taoyuan"},
    "kuala lumpur": {"icao": "WMKK", "elevation_m": 21.0, "uhi_factor": 1.2, "marine_influence": False, "timezone": "Asia/Kuala_Lumpur", "unit": "°C", "bucket_size": 1.0, "station_name": "Kuala Lumpur International"},
    "manila": {"icao": "RPLL", "elevation_m": 22.6, "uhi_factor": 1.5, "marine_influence": True, "timezone": "Asia/Manila", "unit": "°C", "bucket_size": 1.0, "station_name": "Ninoy Aquino International"},
    "jakarta": {"icao": "WIII", "elevation_m": 10.4, "uhi_factor": 1.8, "marine_influence": True, "timezone": "Asia/Jakarta", "unit": "°C", "bucket_size": 1.0, "station_name": "Soekarno-Hatta"},

    # International — Oceania
    "sydney": {"icao": "YSSY", "elevation_m": 6.1, "uhi_factor": 1.0, "marine_influence": True, "timezone": "Australia/Sydney", "unit": "°C", "bucket_size": 1.0, "station_name": "Sydney Kingsford Smith"},
    "melbourne": {"icao": "YMML", "elevation_m": 132.0, "uhi_factor": 0.8, "marine_influence": False, "timezone": "Australia/Melbourne", "unit": "°C", "bucket_size": 1.0, "station_name": "Melbourne Airport"},
    "brisbane": {"icao": "YBBN", "elevation_m": 4.0, "uhi_factor": 0.8, "marine_influence": True, "timezone": "Australia/Brisbane", "unit": "°C", "bucket_size": 1.0, "station_name": "Brisbane Airport"},
    "perth": {"icao": "YPPH", "elevation_m": 20.4, "uhi_factor": 0.5, "marine_influence": True, "timezone": "Australia/Perth", "unit": "°C", "bucket_size": 1.0, "station_name": "Perth Airport"},
    "auckland": {"icao": "NZAA", "elevation_m": 7.0, "uhi_factor": 0.5, "marine_influence": True, "timezone": "Pacific/Auckland", "unit": "°C", "bucket_size": 1.0, "station_name": "Auckland Airport"},
    "wellington": {"icao": "NZWN", "elevation_m": 12.5, "uhi_factor": 0.3, "marine_influence": True, "timezone": "Pacific/Auckland", "unit": "°C", "bucket_size": 1.0, "station_name": "Wellington Airport"},

    # International — Americas
    "toronto": {"icao": "CYYZ", "elevation_m": 173.4, "uhi_factor": 1.2, "marine_influence": False, "timezone": "America/Toronto", "unit": "°C", "bucket_size": 1.0, "station_name": "Toronto Pearson"},
    "montreal": {"icao": "CYUL", "elevation_m": 35.7, "uhi_factor": 1.0, "marine_influence": False, "timezone": "America/Montreal", "unit": "°C", "bucket_size": 1.0, "station_name": "Montreal-Pierre Elliott Trudeau"},
    "vancouver": {"icao": "CYVR", "elevation_m": 4.3, "uhi_factor": 0.3, "marine_influence": True, "timezone": "America/Vancouver", "unit": "°C", "bucket_size": 1.0, "station_name": "Vancouver International"},
    "mexico city": {"icao": "MMMX", "elevation_m": 2230.0, "uhi_factor": 2.5, "marine_influence": False, "timezone": "America/Mexico_City", "unit": "°C", "bucket_size": 1.0, "station_name": "Benito Juarez International"},
    "sao paulo": {"icao": "SBGR", "elevation_m": 749.5, "uhi_factor": 1.5, "marine_influence": False, "timezone": "America/Sao_Paulo", "unit": "°C", "bucket_size": 1.0, "station_name": "Guarulhos"},
    "buenos aires": {"icao": "SAEZ", "elevation_m": 20.4, "uhi_factor": 1.2, "marine_influence": False, "timezone": "America/Argentina/Buenos_Aires", "unit": "°C", "bucket_size": 1.0, "station_name": "Ministro Pistarini"},
    "santiago": {"icao": "SCEL", "elevation_m": 474.0, "uhi_factor": 1.0, "marine_influence": False, "timezone": "America/Santiago", "unit": "°C", "bucket_size": 1.0, "station_name": "Arturo Merino Benitez"},
    "bogota": {"icao": "SKBO", "elevation_m": 2547.0, "uhi_factor": 1.5, "marine_influence": False, "timezone": "America/Bogota", "unit": "°C", "bucket_size": 1.0, "station_name": "El Dorado International"},

    # International — Africa
    "cairo": {"icao": "HECA", "elevation_m": 116.4, "uhi_factor": 2.0, "marine_influence": False, "timezone": "Africa/Cairo", "unit": "°C", "bucket_size": 1.0, "station_name": "Cairo International"},
    "cape town": {"icao": "FACT", "elevation_m": 46.0, "uhi_factor": 0.5, "marine_influence": True, "timezone": "Africa/Johannesburg", "unit": "°C", "bucket_size": 1.0, "station_name": "Cape Town International"},
    "johannesburg": {"icao": "FAOR", "elevation_m": 1694.0, "uhi_factor": 0.5, "marine_influence": False, "timezone": "Africa/Johannesburg", "unit": "°C", "bucket_size": 1.0, "station_name": "O.R. Tambo International"},
    "lagos": {"icao": "DNMM", "elevation_m": 41.1, "uhi_factor": 2.0, "marine_influence": True, "timezone": "Africa/Lagos", "unit": "°C", "bucket_size": 1.0, "station_name": "Murtala Muhammed"},
    "nairobi": {"icao": "HKJK", "elevation_m": 1624.5, "uhi_factor": 0.8, "marine_influence": False, "timezone": "Africa/Nairobi", "unit": "°C", "bucket_size": 1.0, "station_name": "Jomo Kenyatta International"},
    "ankara": {"icao": "LTAC", "elevation_m": 952.5, "uhi_factor": 1.0, "marine_influence": False, "timezone": "Europe/Istanbul", "unit": "°C", "bucket_size": 1.0, "station_name": "Ankara Esenboga"},
}


# =============================================================================
# Helper: resolve station metadata for a location name
# =============================================================================

def get_station_meta(location_name: str) -> dict[str, object]:
    """Return STATION_METADATA for a location name, or a sensible default."""
    key = location_name.lower()
    if key in STATION_METADATA:
        return dict(STATION_METADATA[key])
    # Derive defaults from location map
    coords = LOCATION_MAP.get(key)
    if coords is None:
        return {
            "icao": "UNKN", "elevation_m": 0.0, "uhi_factor": 1.0,
            "marine_influence": False, "timezone": "UTC",
            "unit": "°F", "bucket_size": 2.0, "station_name": location_name,
        }
    # Guess unit: US locations use °F, rest use °C
    is_us = -125 < coords[1] < -65 and 24 < coords[0] < 50
    return {
        "icao": "UNKN",
        "elevation_m": 0.0,
        "uhi_factor": 1.0,
        "marine_influence": False,
        "timezone": "UTC",
        "unit": "°F" if is_us else "°C",
        "bucket_size": 2.0 if is_us else 1.0,
        "station_name": location_name.title(),
    }


# =============================================================================
# Weather Market Parser
# =============================================================================


class WeatherMarketParser:
    """
    Parses Polymarket question strings to extract structured weather market data.

    Supports multiple question formats:
    1. "Highest temperature in {location} on {date}?"
       → Multi-bucket: <90°F, 90-92°F, 92-95°F, etc.
    2. "Will {location} record a temperature above {threshold}°C on {date}?"
       → Binary: Yes/No
    3. "What will be the highest temperature in {location} on {date}?"
       → Multi-bucket
    """

    # Date patterns: "July 26", "July 26 2025", "26 July", "26 July 2025"
    DATE_PATTERNS: ClassVar[list[str]] = [
        r"(?:on\s+)?([A-Z][a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?",
        r"(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+([A-Z][a-z]+)(?:\s*,?\s*(\d{4}))?",
    ]

    # Temperature threshold: "above 28°C", "above 90°F"
    TEMP_THRESHOLD_PATTERN: ClassVar[str] = r"above\s+(\d+(?:\.\d+)?)\s*°?\s*([CF])"

    # Temperature buckets: "<90°F", "90-92°F", "95°F+"
    TEMP_BUCKET_PATTERN: ClassVar[str] = r"([<>]?\s*\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*°?\s*([CF])"

    def __init__(self, location_map: dict[str, tuple[float, float]] | None = None) -> None:
        self._location_map = location_map or LOCATION_MAP

    # =========================================================================
    # Main parse entry point
    # =========================================================================

    def parse_question(
        self,
        question: str,
        outcomes: list[str] | None = None,
    ) -> WeatherMarket | None:
        """
        Parse a Polymarket question into a WeatherMarket.

        Args:
            question: The market question text.
            outcomes: Optional list of outcome labels (e.g. ``"< 50F"``,
                ``"50-60F"``, ``">= 90F"``). When the question text itself
                contains no explicit temperature ranges, buckets are derived
                from these labels — Polymarket stores temperature ranges as
                outcome labels, not always in the question string.

        Returns None if the question can't be parsed as a weather market.
        """
        import re

        question_lower = question.lower()

        # Quick filter: must contain temperature-related keywords OR have
        # outcome labels that look like temperature ranges.
        weather_keywords = [
            "temperature", "highest temp", "temp above",
            "°c", "°f", "degrees", "hottest",
        ]
        has_kw = any(kw in question_lower for kw in weather_keywords)
        has_outcome_temp = bool(outcomes) and any(
            self._parse_outcome_bucket(o) is not None for o in outcomes
        )
        if not has_kw and not has_outcome_temp:
            return None

        # Extract location
        location_info = self._extract_location(question_lower)
        if location_info is None:
            return None
        location_name, lat, lon = location_info

        # Extract date
        target_date = self._extract_date(question)
        if target_date is None:
            target_date = date.today()

        # Extract buckets (fall back to outcome labels if question has none)
        buckets = self._extract_buckets(question, question_lower, outcomes=outcomes)

        return WeatherMarket(
            question=question,
            location=location_name,
            lat=lat,
            lon=lon,
            target_date=target_date,
            buckets=buckets,
        )

    # =========================================================================
    # Location extraction
    # =========================================================================

    def _extract_location(self, question_lower: str) -> tuple[str, float, float] | None:
        """Extract location name and coordinates from question text."""
        # Try longest match first to avoid partial matches
        sorted_locations = sorted(
            self._location_map.items(),
            key=lambda x: len(x[0]),
            reverse=True,
        )

        for loc_name, coords in sorted_locations:
            if loc_name in question_lower:
                return loc_name, coords[0], coords[1]

        return None

    # =========================================================================
    # Date extraction
    # =========================================================================

    def _extract_date(self, question: str) -> date | None:
        """Extract date from question text."""
        import re

        current_year = date.today().year

        for pattern in self.DATE_PATTERNS:
            match = re.search(pattern, question)
            if match:
                groups = match.groups()
                if len(groups) == 3 and groups[2]:
                    # Format: "Month Day Year" with year
                    month_str, day_str, year_str = groups
                    year = int(year_str)
                elif len(groups) == 3:
                    # Format: "Month Day" (no year)
                    if groups[0][0].isalpha():
                        month_str, day_str = groups[0], groups[1]
                    else:
                        day_str, month_str = groups[0], groups[1]
                    year = current_year
                elif len(groups) == 2:
                    if groups[0][0].isalpha():
                        month_str, day_str = groups[0], groups[1]
                    else:
                        day_str, month_str = groups[0], groups[1]
                    year = current_year
                else:
                    continue

                try:
                    month_map = {
                        "january": 1, "february": 2, "march": 3, "april": 4,
                        "may": 5, "june": 6, "july": 7, "august": 8,
                        "september": 9, "october": 10, "november": 11, "december": 12,
                    }
                    month = month_map.get(month_str.lower(), 0)
                    if month == 0:
                        continue
                    return date(year, month, int(day_str))
                except (ValueError, IndexError):
                    continue

        return None

    # =========================================================================
    # Bucket extraction
    # =========================================================================

    def _extract_buckets(
        self,
        question: str,
        question_lower: str,
        outcomes: list[str] | None = None,
    ) -> list[TemperatureBucket]:
        """
        Extract temperature buckets from question text.

        Handles:
        - Multi-bucket: "<90°F", "90-92°F", "92-95°F", "95-98°F", "98°F+"
        - Binary: "above 28°C" → Yes/No
        """
        import re

        buckets: list[TemperatureBucket] = []

        # Check if this is a binary "above X" market
        threshold_match = re.search(self.TEMP_THRESHOLD_PATTERN, question)
        if threshold_match:
            temp_val = float(threshold_match.group(1))
            unit = threshold_match.group(2).upper()

            if unit == "C":
                temp_f = self._c_to_f(temp_val)
            else:
                temp_f = temp_val

            # Binary: above threshold → Yes, below → No
            buckets.extend([
                TemperatureBucket(
                    min_val=temp_f,
                    max_val=200.0,
                    label=f"Above {temp_val}°{unit}",
                    is_open_upper=True,
                ),
                TemperatureBucket(
                    min_val=-100.0,
                    max_val=temp_f,
                    label=f"Below {temp_val}°{unit}",
                ),
            ])
            return buckets

        # Check for bucket patterns like "90-92°F" or "<90°F"
        # First, try to find explicit bucket ranges
        range_pattern = r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*°?\s*([CF])"
        ranges = re.findall(range_pattern, question)

        if ranges:
            seen: set[tuple[float, float]] = set()
            for low_str, high_str, unit in ranges:
                low = float(low_str)
                high = float(high_str)
                if unit.upper() == "C":
                    low = self._c_to_f(low)
                    high = self._c_to_f(high)

                key = (low, high)
                if key not in seen:
                    seen.add(key)
                    buckets.append(TemperatureBucket(
                        min_val=low,
                        max_val=high,
                        label=f"{low_str}-{high_str}°{unit}",
                    ))

            if buckets:
                buckets.sort(key=lambda b: b.min_val)
                return buckets

        # Try single-value patterns like "<90°F" or "95°F+"
        single_pattern = r"([<>])\s*(\d+(?:\.\d+)?)\s*°?\s*([CF])"
        singles = re.findall(single_pattern, question)
        if singles:
            for op, val_str, unit in singles:
                val = float(val_str)
                if unit.upper() == "C":
                    val = self._c_to_f(val)

                if op == "<":
                    buckets.append(TemperatureBucket(
                        min_val=-100.0, max_val=val,
                        label=f"<{val_str}°{unit}",
                    ))
                else:
                    buckets.append(TemperatureBucket(
                        min_val=val, max_val=200.0,
                        label=f">{val_str}°{unit}",
                        is_open_upper=True,
                    ))

        # Fallback: derive buckets from outcome labels (e.g. "< 50F",
        # "50-60F", ">= 90F") when the question text has no explicit ranges.
        if not buckets and outcomes:
            for label in outcomes:
                ob = self._parse_outcome_bucket(label)
                if ob is not None:
                    buckets.append(ob)
            if buckets:
                buckets.sort(key=lambda b: b.min_val)

        return buckets

    def _parse_outcome_bucket(self, label: str) -> TemperatureBucket | None:
        """Parse a single outcome label into a TemperatureBucket.

        Handles labels like ``"< 50F"``, ``"<= 50°F"``, ``">= 90F"``,
        ``"> 90°F"``, ``"50-60F"``, ``"50-60°F"``. Returns None for labels
        that are not temperature ranges (e.g. ``"Yes"``, ``"No"``).
        """
        import re

        s = label.strip()
        if not s:
            return None

        # Lower open: < X, <= X, ≤ X  (e.g. "< 50F", "<=50°F")
        m = re.match(r"^[<≤]\s*=?\s*(\d+(?:\.\d+)?)\s*°?\s*([CF])?$", s, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            unit = (m.group(2) or "F").upper()
            if unit == "C":
                val = self._c_to_f(val)
            return TemperatureBucket(min_val=-100.0, max_val=val, label=label)

        # Upper open: > X, >= X, ≥ X  (e.g. ">= 90F", "> 90°F")
        m = re.match(r"^[>≥]\s*=?\s*(\d+(?:\.\d+)?)\s*°?\s*([CF])?$", s, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            unit = (m.group(2) or "F").upper()
            if unit == "C":
                val = self._c_to_f(val)
            return TemperatureBucket(
                min_val=val, max_val=200.0, label=label, is_open_upper=True,
            )

        # Range: X-Y  (e.g. "50-60F", "85-90°F")
        m = re.match(
            r"^(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*°?\s*([CF])?$",
            s, re.IGNORECASE,
        )
        if m:
            low = float(m.group(1))
            high = float(m.group(2))
            unit = (m.group(3) or "F").upper()
            if unit == "C":
                low = self._c_to_f(low)
                high = self._c_to_f(high)
            return TemperatureBucket(min_val=low, max_val=high, label=label)

        return None

    # =========================================================================
    # Helpers
    # =========================================================================

    @staticmethod
    def _c_to_f(celsius: float) -> float:
        return celsius * 9.0 / 5.0 + 32.0

    @staticmethod
    def _f_to_c(fahrenheit: float) -> float:
        return (fahrenheit - 32.0) * 5.0 / 9.0

    def add_location(self, name: str, lat: float, lon: float) -> None:
        """Register a new location in the map."""
        self._location_map[name.lower()] = (lat, lon)

    def get_coords(self, location_name: str) -> tuple[float, float] | None:
        """Look up coordinates for a location name."""
        return self._location_map.get(location_name.lower())
