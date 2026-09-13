"""
High-level domain workflows and service orchestrators.
"""

from musicscraper.services.reconciler import DiscographyReconciler, deduplicate_candidate_tracks
from musicscraper.services.auditor import AuditorService, AudioFileScanner
from musicscraper.services.soulseek import SlskdArtistScraper, PeerCandidateIndex, CandidateDir, CandidateFile
from musicscraper.services.artist import ArtistDownloadOrchestrator
from musicscraper.services.library import LibraryReleaseService

__all__ = [
    "DiscographyReconciler",
    "deduplicate_candidate_tracks",
    "AuditorService",
    "AudioFileScanner",
    "SlskdArtistScraper",
    "PeerCandidateIndex",
    "CandidateDir",
    "CandidateFile",
    "ArtistDownloadOrchestrator",
    "LibraryReleaseService",
]

