import setproctitle

PROC_PREFIX = "citadel-"


def name_process(label: str) -> None:
    setproctitle.setproctitle(f"{PROC_PREFIX}{label}")
