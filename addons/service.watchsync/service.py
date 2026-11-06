import xbmc
import xbmcaddon

ADDON = xbmcaddon.Addon()
ADDON_NAME = ADDON.getAddonInfo("name")


def log(msg, level=xbmc.LOGINFO):
    xbmc.log(f"[{ADDON_NAME}] {msg}", level)


class PlayerMonitor(xbmc.Player):
    def onPlayBackStarted(self):
        log("Playback started")

    def onPlayBackStopped(self):
        log("Playback stopped")

    def onPlayBackEnded(self):
        log("Playback ended")


if __name__ == "__main__":
    monitor = xbmc.Monitor()
    player = PlayerMonitor()
    log("Service started")
    while not monitor.abortRequested():
        if monitor.waitForAbort(10):
            break
    log("Service stopped")
