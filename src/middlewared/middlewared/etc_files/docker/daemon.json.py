import json
import os
import subprocess

from middlewared.plugins.etc import FileShouldNotExist
from middlewared.plugins.docker.state_utils import IX_APPS_MOUNT_PATH
from middlewared.utils.gpu import get_nvidia_gpus


def render(service, middleware):
    config = middleware.call_sync('docker.config')
    http_proxy = middleware.call_sync('network.configuration.config')['httpproxy']
    if not config['pool']:
        raise FileShouldNotExist()

    # We need to do this so that proxy changes are respected by systemd on docker daemon start
    subprocess.run(['systemctl', 'daemon-reload'], capture_output=True, check=True)

    os.makedirs('/etc/docker', exist_ok=True)
    data_root = os.path.join(IX_APPS_MOUNT_PATH, 'docker')
    base = {
        'data-root': data_root,
        'exec-opts': ['native.cgroupdriver=cgroupfs'],
        'iptables': True,
        'ipv6': True,
        'storage-driver': 'overlay2',
        'fixed-cidr-v6': config['cidr_v6'],
        'default-address-pools': config['address_pools'],
        **(
            {
                'proxies': {
                    'http-proxy': http_proxy,
                    'https-proxy': http_proxy,
                }
            } if http_proxy else {}
        )
    }
    isolated = middleware.call_sync('system.advanced.config')['isolated_gpu_pci_ids']
    for gpu in filter(lambda x: x not in isolated, get_nvidia_gpus()):
        base.update({
            'runtimes': {
                'nvidia': {
                    'path': '/usr/bin/nvidia-container-runtime',
                    'runtimeArgs': []
                }
            },
            'default-runtime': 'nvidia',
        })
        break

    docker_registry_mirror_json_path = '/root/docker-registry-mirrors.json'
    if os.path.exists(docker_registry_mirror_json_path):
        with open(docker_registry_mirror_json_path, mode='r') as f:
            try:
                mirror_arr = json.load(f)
            except:
                mirror_arr = []
            if isinstance(mirror_arr, list) and len(mirror_arr) > 0:
                base.update({'registry-mirrors': mirror_arr})
    else:
        with open(docker_registry_mirror_json_path, mode='w') as f:
            json.dump([], f)

    return json.dumps(base)
