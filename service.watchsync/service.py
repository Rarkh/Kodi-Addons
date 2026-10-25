import xbmc
import xbmcaddon

ADDON = xbmcaddon.Addon()
ADDON_NAME = ADDON.getAddonInfo("name")


def log(msg, level=xbmc.LOGINFO):
    xbmc.log(f"[{ADDON_NAME}] {msg}", level)


class PlayerMonitor(xbmc.Player):
    def onPlayBackStarted(self):
        log("Wiedergabe gestartet")

    def onPlayBackStopped(self):
        log("Wiedergabe gestoppt")

    def onPlayBackEnded(self):
        log("Wiedergabe beendet")


if __name__ == "__main__":
    monitor = xbmc.Monitor()
    player = PlayerMonitor()
    log("Dienst gestartet")
    while not monitor.abortRequested():
        if monitor.waitForAbort(10):
            break
    log("Dienst beendet")
