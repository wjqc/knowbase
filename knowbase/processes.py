"""Hide Windows console children while preserving their pipes and exit status."""
import os


def no_window():
    return {'creationflags':0x08000000} if os.name=='nt' else {}
