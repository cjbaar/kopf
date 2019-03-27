"""
Peer monitoring: knowing which other operators do run, and exchanging the basic signals with them.

The main use-case is to suppress all deployed operators when a developer starts a dev-/debug-mode
operator for the same cluster on their workstation -- to avoid any double-processing.

See also: `kopf freeze` & `kopf resume` CLI commands for the same purpose.

WARNING: There are **NO** per-object locks between the operators, so only one operator
should be functional for the cluster, i.e. only one with the highest priority running.
If the operator sees the violations of this constraint, it will print the warnings
pointing to another same-priority operator, but will continue to function.

The "signals" exchanged are only the keep-alive notifications from the operator being alive,
and detection of other operators hard termination (by timeout rather than by clear exit).

The peers monitoring covers both the in-cluster operators running,
and the dev-mode operators running in the dev workstations.

For this, a special CRD ``kind: KopfPeering`` (cluster-scoped) should be registered
in the cluster, and its status is used by all the operators to sync their keep-alive info.

The namespace-bound operators (e.g. `--namespace=`) report their individual
namespaces are part of the payload, can see all other cluster and namespaced
operators (even from the different namespaces), and behave accordingly.

The CRD is not applied automatically, so you have to deploy it yourself explicitly.
To disable the peers monitoring, use the `--standalone` CLI option.
"""

import asyncio
import datetime
import getpass
import logging
import os
import random
import socket
from typing import Optional, Mapping, Iterable

import iso8601
import kubernetes

from kopf.reactor.registry import Resource

logger = logging.getLogger(__name__)

# The CRD info on the special sync-object.
PEERING_CRD_RESOURCE = Resource('zalando.org', 'v1', 'kopfpeerings')
PEERING_DEFAULT_NAME = 'default'


# The class used to represent a peer in the parsed peers list (for convenience).
# The extra fields are for easier calculation when and if the peer is dead to the moment.
class Peer:

    def __init__(self,
                 id: str, *,
                 peering: str,
                 priority: int = 0,
                 lastseen: Optional[str] = None,
                 lifetime: int = 60,
                 namespace: Optional[str] = None,
                 **kwargs):  # for the forward-compatibility with the new fields
        super().__init__()
        self.id = id
        self.peering = peering
        self.namespace = namespace
        self.priority = (priority)
        self.lifetime = (lifetime if isinstance(lifetime, datetime.timedelta) else
                         datetime.timedelta(seconds=int(lifetime)))
        self.lastseen = (lastseen if isinstance(lastseen, datetime.datetime) else
                         iso8601.parse_date(lastseen) if lastseen is not None else
                         datetime.datetime.utcnow())
        self.lastseen = self.lastseen.replace(tzinfo=None)  # only the naive utc -- for comparison
        self.deadline = self.lastseen + self.lifetime
        self.is_dead = self.deadline <= datetime.datetime.utcnow()

    def __repr__(self):
        return f"{self.__class__.__name__}({self.id}, namespace={self.namespace}, priority={self.priority}, lastseen={self.lastseen}, lifetime={self.lifetime})"

    def as_dict(self):
        # Only the non-calculated and non-identifying fields.
        return {
            'namespace': self.namespace,
            'priority': self.priority,
            'lastseen': self.lastseen.isoformat(),
            'lifetime': self.lifetime.total_seconds(),
        }

    def touch(self, *, lifetime: Optional[int] = None):
        self.lastseen = datetime.datetime.utcnow()
        self.lifetime = (self.lifetime if lifetime is None else
                         lifetime if isinstance(lifetime, datetime.timedelta) else
                         datetime.timedelta(seconds=int(lifetime)))
        self.deadline = self.lastseen + self.lifetime
        self.is_dead = self.deadline <= datetime.datetime.utcnow()

    def keepalive(self):
        """
        Add a peer to the peers, and update its alive status.
        """
        self.touch()
        apply_peers([self], peering=self.peering)

    def disappear(self):
        """
        Remove a peer from the peers (gracefully).
        """
        self.touch(lifetime=0)
        apply_peers([self], peering=self.peering)


def apply_peers(
        peers: Iterable[Peer],
        peering: str,
):
    """
    Apply the changes in the peers to the sync-object.

    The dead peers are removed, the new or alive peers are stored.
    Note: this does NOT change their `lastseen` field, so do it explicitly with ``touch()``.
    """
    api = kubernetes.client.CustomObjectsApi()
    api.patch_cluster_custom_object(
        group=PEERING_CRD_RESOURCE.group,
        version=PEERING_CRD_RESOURCE.version,
        plural=PEERING_CRD_RESOURCE.plural,
        name=peering,
        body={'status': {peer.id: None if peer.is_dead else peer.as_dict() for peer in peers}},
    )


async def peers_handler(
        *,
        event: Mapping,
        freeze: asyncio.Event,
        ourselves: Peer,
        autoclean: bool = True,
):
    """
    Handle a single update of the peers by us or by other operators.

    When an operator with a higher priority appears, switch to the freeze-mode.
    The these operators disappear or become presumably dead, resume the event handling.

    The `freeze` object is passed both to the peers handler to set/clear it,
    and to all the resource handlers to check its value when the events arrive
    (see `create_tasks` and `run` functions).
    """

    # Silently ignore the peering objects which are not ours to worry.
    body = event['object']
    name = body.get('metadata', {}).get('name', None)
    if name != ourselves.peering:
        return

    # Find if we are still the highest priority operator.
    pairs = body.get('status', {}).items()
    peers = [Peer(id=opid, peering=name, **opinfo) for opid, opinfo in pairs]
    dead_peers = [peer for peer in peers if peer.is_dead]
    prio_peers = [peer for peer in peers if not peer.is_dead and peer.priority > ourselves.priority]
    same_peers = [peer for peer in peers if not peer.is_dead and peer.priority == ourselves.priority and peer.id != ourselves.id]

    if autoclean and dead_peers:
        apply_peers(dead_peers, peering=ourselves.peering)  # NB: sync and blocking, but this is fine.

    if prio_peers:
        if not freeze.is_set():
            logger.info(f"Freezing operations in favour of {prio_peers}.")
            freeze.set()
    else:
        if same_peers:
            logger.warning(f"Possibly conflicting operators with the same priority: {same_peers}.")
        if freeze.is_set():
            logger.info(f"Resuming operations after the freeze.")
            freeze.clear()


async def peers_keepalive(
        *,
        ourselves: Peer,
):
    """
    An ever-running coroutine to regularly send our own keep-alive status for the peers.
    """
    try:
        while True:
            logger.debug(f"Peering keep-alive update for {ourselves.id} (priority {ourselves.priority})")
            ourselves.keepalive()

            # How often do we update. Keep limited to avoid k8s api flooding.
            # Should be slightly less than the lifetime, enough for a patch request to finish.
            await asyncio.sleep(max(1, ourselves.lifetime.total_seconds()-10))
    finally:
        try:
            ourselves.disappear()
        except:
            pass


def detect_own_id() -> str:
    """
    Detect or generate the id for ourselves, i.e. the execute operator.

    It is constructed easy to detect in which pod it is running
    (if in the cluster), or who runs the operator (if not in the cluster,
    i.e. in the dev-mode), and how long ago was it started.

    The pod id can be specified by::

        env:
        - name: POD_ID
          valueFrom:
            fieldRef:
              fieldPath: metadata.name

    Used in the `kopf.reactor.queueing` when the reactor starts,
    but is kept here, close to the rest of the peering logic.
    """

    pod = os.environ.get('POD_ID', None)
    if pod is not None:
        return pod

    user = getpass.getuser()
    host = socket.getfqdn()
    now = datetime.datetime.utcnow().isoformat()
    rnd = ''.join(random.choices('abcdefhijklmnopqrstuvwxyz0123456789', k=6))
    return f'{user}@{host}/{now}/{rnd}'
