from middlewared.service import Service

from .ix_apps.utils import get_app_name_from_project_name
from .utils import get_app_stop_cache_key


PROCESSING_APP_EVENT = set()


class AppEvents(Service):

    class Config:
        namespace = 'app.events'
        private = True

    async def process(self, app_name, container_event):
        cache_key = get_app_stop_cache_key(app_name)
        if (app := await self.middleware.call('app.query', [['id', '=', app_name]])) and not await self.middleware.call(
            'cache.has_key', cache_key
        ):
            self.middleware.send_event(
                'app.query', 'CHANGED', id=app_name, fields=app[0],
            )


async def app_event(middleware, event_type, args):
    app_name = get_app_name_from_project_name(args['id'])
    if app_name in PROCESSING_APP_EVENT:
        return

    PROCESSING_APP_EVENT.add(app_name)

    try:
        await middleware.call('app.events.process', app_name, args['fields'])
    except Exception as e:
        middleware.logger.warning('Unhandled exception: %s', e)
    finally:
        PROCESSING_APP_EVENT.remove(app_name)


async def setup(middleware):
    middleware.event_subscribe('docker.events', app_event)
