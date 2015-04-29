#    pubcontrol.py
#    ~~~~~~~~~
#    This module implements the PubControl class.
#    :authors: Justin Karneges, Konstantin Bokarius.
#    :copyright: (c) 2015 by Fanout, Inc.
#    :license: MIT, see LICENSE for more details.

import threading
from .pcccbhandler import PubControlClientCallbackHandler
from .pubcontrolclient import PubControlClient
from .zmqpubcontrolclient import ZmqPubControlClient
from .utilities import _ensure_utf8
from .zmqsubmonitor import ZmqSubMonitor

try:
	import zmq
except ImportError:
	zmq = None

try:
	import tnetstring
except ImportError:
	tnetstring = None

# The PubControl class allows a consumer to manage a set of publishing
# endpoints and to publish to all of those endpoints via a single publish
# or publish_async method call. A PubControl instance can be configured
# either using a hash or array of hashes containing configuration information
# or by manually adding either PubControlClient or ZmqPubControlClient
# instances.
class PubControl(object):

	# Initialize with or without a configuration. A configuration can be applied
	# after initialization via the apply_config method. Optionally specify a
	# subscription callback method that will be executed whenever a channel is 
	# subscribed to or unsubscribed from. The callback accepts two parameters:
	# the first parameter a string containing 'sub' or 'unsub' and the second
	# parameter containing the channel name. Optionally specify a ZMQ context
	# to use otherwise the global ZMQ context will be used.
	def __init__(self, config=None, sub_callback=None, zmq_context=None):
		self._lock = threading.Lock()
		self._sub_callback = sub_callback
		self._zmq_sub_monitor = None
		self._zmq_sock = None
		self._zmq_ctx = None
		if zmq_context:
			self._zmq_ctx = zmq_context
		elif zmq:
			self._zmq_ctx = zmq.Context.instance()
		self.clients = list()
		if config:
			self.apply_config(config)

	# Remove all of the configured client instances and close all open ZMQ sockets.
	def remove_all_clients(self):
		for client in self.clients:
			if not isinstance(client, PubControlClient):
				client.close()
		self.clients = list()
		if self._zmq_sock:
			self._lock.acquire()
			self._zmq_sock.close()
			self._zmq_sock = None
			self._lock.release()

	# Add the specified PubControlClient or ZmqPubControlClient instance.
	def add_client(self, client):
		self.clients.append(client)

	# Apply the specified configuration to this PubControl instance. The
	# configuration object can either be a hash or an array of hashes where
	# each hash corresponds to a single PubControlClient or ZmqPubControlClient
	# instance. Each hash will be parsed and a client instance will be created.
	# Specify a 'uri' hash key along with optional JWT authentication 'iss' and
	# 'key' hash keys for a PubControlClient configuration. Specify a combination
	# of 'zmq_uri', 'zmq_pub_uri', or 'zmq_push_uri' hash keys and an optional
	# 'zmq_require_subscribers' hash key for a ZmqPubControlClient configuration.
	def apply_config(self, config):
		if not isinstance(config, list):
			config = [config]
		for entry in config:
			if (entry.get('zmq_require_subscribers') is False and
					'zmq_pub_uri' not in entry):
				raise ValueError('zmq_pub_uri must be set if require_subscriptions ' +
						'is set to true')
		for entry in config:
			client = None
			if 'uri' in entry:
				client = PubControlClient(entry['uri'])
				if 'iss' in entry:
					client.set_auth_jwt({'iss': entry['iss']}, entry['key'])
			if ('zmq_uri' in entry or 'zmq_push_uri' in entry or
					'zmq_pub_uri' in entry):
				if zmq is None:
					raise ValueError('zmq package must be installed')
				if tnetstring is None:
					raise ValueError('tnetstring package must be installed')
				require_subscribers = entry.get('zmq_require_subscribers')
				if require_subscribers is None:
					require_subscribers = False
				client = ZmqPubControlClient(entry.get('zmq_uri'),
						entry.get('zmq_push_uri'), entry.get('zmq_pub_uri'),
						require_subscribers, True, None, self._zmq_ctx)
				if ('zmq_pub_uri' in entry and
						(require_subscribers or 'zmq_push_uri' not in entry)):
					self._connect_zmq_pub_uri(entry['zmq_pub_uri'])
			if client:
				self.clients.append(client)

	# The publish method for publishing the specified item to the specified
	# channel on the configured endpoint. The blocking parameter indicates
	# whether the call should be blocking or non-blocking. The callback method
	# is optional and will be passed the publishing results after publishing is
	# complete. Note that a failure to publish in any of the configured
	# client instances will result in a failure result being passed to the
	# callback method along with the first encountered error message.
	def publish(self, channel, item, blocking=False, callback=None):
		self._send_to_zmq(channel, item)
		if blocking:
			for client in self.clients:
				client.publish(channel, item, blocking=True)
		else:
			if callback is not None:
				cb = PubControlClientCallbackHandler(len(self.clients), callback).handler
			else:
				cb = None

			for client in self.clients:
				client.publish(channel, item, blocking=False, callback=cb)

	# The finish method is a blocking method that ensures that all asynchronous
	# publishing is complete for all of the configured client instances prior to
	# returning and allowing the consumer to proceed.
	def finish(self):
		for client in self.clients:
			if not isinstance(client, ZmqPubControlClient):
				client.finish()

	# An internal method for connecting to a ZMQ PUB URI. If necessary a ZMQ
	# PUB socket will be created.
	def _connect_zmq_pub_uri(self, uri):
		self._lock.acquire()
		if self._zmq_sock is None:
			self._zmq_sock = self._zmq_ctx.socket(zmq.XPUB)
			self._zmq_sock.linger = 0
			if self._sub_callback:
				self._zmq_sub_monitor = ZmqSubMonitor(self._zmq_sock,
						self._lock, self._sub_callback)
		self._zmq_sock.connect(uri)
		self._lock.release()

	# An internal method for sending a ZMQ message to the configured ZMQ PUB
	# socket and specified channel.
	def _send_to_zmq(self, channel, item):
		self._lock.acquire()
		if self._zmq_sock is not None:
			channel = _ensure_utf8(channel)
			content = item.export(True, True)
			self._zmq_sock.send_multipart([channel, tnetstring.dumps(content)])
		self._lock.release()

	# An internal method used as a callback for the PUB socket ZmqSubMonitor
	# instance. The purpose of this callback is to aggregate sub and unsub
	# events coming from the monitor and any clients that have their own
	# monitor. The consumer's sub_callback is executed when a channel is
	# first subscribed to for the first time across any clients or when a
	# channel is unsubscribed to across all clients.
	# NOTE: This method assumes that a subscription monitor instance will
	# execute the callback: 1) before adding a subscription to its list
	# upon a 'sub' event, and 2) after removing a subscription from its
	# list upon an 'unsub' event.
	def _submonitor_callback(eventType, chan):
		self._lock.acquire()
		executeCallback = True
		for client in self.clients:
			if client._sub_monitor is not None:
				if chan in client._sub_monitor.subscriptions:
					executeCallback = False
					break
		if (executeCallback and
				chan not in _self._sub_monitor.subscriptions):
			self._sub_callback(eventType, chan)
		self._lock.release()
