import json

from aiohttp import ClientResponseError, ClientSession, ClientTimeout

from middlewared.service import CallError, private, Service
from middlewared.utils.network import INTERNET_TIMEOUT
from middlewared.utils.functools_ import cache
from .utils import can_update, scale_update_server, SCALE_MANIFEST_FILE


class UpdateService(Service):
    opts = {'raise_for_status': True, 'trust_env': True, 'timeout': ClientTimeout(INTERNET_TIMEOUT)}
    update_srv = scale_update_server()

    @private
    @cache
    def get_manifest_file(self):
        with open(SCALE_MANIFEST_FILE) as f:
            return json.load(f)

    @private
    async def fetch(self, url):
        async with ClientSession(**self.opts) as client:
            try:
                async with client.get(url) as resp:
                    return await resp.json()
            except ClientResponseError as e:
                raise CallError(f'Error while fetching update manifest: {e}')

    @private
    async def get_scale_update(self, train, current_version):
        # XXX: upstream updates mess up self-built image
        #      and save network traffic
        return {"status": "UNAVAILABLE"}
        new_manifest = await self.fetch(f"{self.update_srv}/{train}/manifest.json")
        if not can_update(current_version, new_manifest["version"]):
            return {"status": "UNAVAILABLE"}

        return {
            "status": "AVAILABLE",
            "changes": [{
                "operation": "upgrade",
                "old": {
                    "name": "TrueNAS",
                    "version": current_version,
                },
                "new": {
                    "name": "TrueNAS",
                    "version": new_manifest["version"],
                }
            }],
            "notice": None,
            "notes": None,
            "release_notes_url": await self.middleware.call("system.release_notes_url", new_manifest["version"]),
            "changelog": new_manifest["changelog"],
            "version": new_manifest["version"],
            "filename": new_manifest["filename"],
            "filesize": new_manifest["filesize"],
            "checksum": new_manifest["checksum"],
        }

    @private
    async def get_trains_data(self):
        return {
            "current_train": (await self.middleware.call("update.get_manifest_file"))["train"],
            **(await self.fetch(f"{self.update_srv}/trains.json"))
        }

    @private
    async def check_train(self, train):
        old_vers = (await self.middleware.call("update.get_manifest_file"))["version"]
        return await self.middleware.call("update.get_scale_update", train, old_vers)
