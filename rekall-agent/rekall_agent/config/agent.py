#!/usr/bin/env python2

# Rekall Memory Forensics
# Copyright 2016 Google Inc. All Rights Reserved.
#
# Author: Michael Cohen scudette@google.com
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
#

__author__ = "Michael Cohen <scudette@google.com>"

"""Defines the basic agent configuration system."""
import json
import os

from rekall import obj
from rekall_agent import action
from rekall_agent import crypto
from rekall_agent import location
from rekall_agent import serializer


class Manifest(serializer.SerializedObject):
    """The manifest contains basic information about the installation.

    It needs to be world readable because clients use this to bootstrap their
    basic information about the server.

    The manifest is signed and stored inside a SignedManifest message below.
    """

    schema = [
        dict(name="startup_actions", type=action.Action, repeated=True,
             doc="These actions will be run at startup on the client."),
    ]


class SignedManifest(serializer.SerializedObject):
    """A signed manifest.

    The agent will verify the manifest's signature.  First we verify the
    server's certificate is properly signed by the hardcoded CA.  Then we verify
    that the manifest is signed using the server's certificate.
    """

    schema = [
        dict(name="data", type="bytes",
             doc="The Json encoded Manifest object."),

        dict(name="signature", type="bytes",
             doc="Signature of the manifest"),

        dict(name="server_certificate", type=crypto.X509Ceritifcate,
             doc="The server's certificate."),
    ]


class ExternalFileMixin(object):
    """This mixin allows parameters to be defined using filter notation.

    Sometimes it is more convenient to specify certain fields come from external
    sources. This mixin allows the following sources:

    field_name@environ <---- reads field_name from the specified environment
    name if this is set.

    field_name@file <----- reads file_name from the specified file if the file
    can be found.
    """

    @classmethod
    def from_primitive(cls, data, session=None):
        if not data:
            data = {}

        # This is the search_path for configuration files.
        search_path = session.GetParameter("config_search_path", ["."])

        result = {}
        for k, v in data.iteritems():
            if "@" in k:
                field_name, filter_name = k.split("@", 1)
                if filter_name == "env":
                    if v in os.environ:
                        session.logging.info(
                            "Fetching %s from env %s", field_name, v)
                        result[field_name] = json.loads(os.environ[v])
                elif filter_name == "file":
                    file_data = cls._locate_file_data_in_search_path(
                        v, search_path)
                    if file_data is None:
                        session.logging.warn(
                            "Unable to find file %s for field %s", v,
                            field_name)
                    else:
                        result[field_name] = file_data

                elif filter_name == "json_file":
                    file_data = cls._locate_file_data_in_search_path(
                        v, search_path)
                    if file_data is None:
                        session.logging.warn(
                            "Unable to find file %s for field %s", v,
                            field_name)
                    else:
                        result[field_name] = json.loads(file_data)

            else:
                result[k] = v

        return super(ExternalFileMixin, cls).from_primitive(
            result, session=session)

    @staticmethod
    def _locate_file_data_in_search_path(path, search_paths):
        # Allow homedir and environment vars to be specified.
        path = os.path.expandvars(os.path.expanduser(path))
        if os.path.isabs(path):
            try:
                return open(path, "rb").read()
            except IOError:
                return
        else:
            for search in search_paths:
                try:
                    path_to_try = os.path.join(search, path)
                    return open(path_to_try, "rb").read()
                except IOError:
                    continue


class ClientWriteback(serializer.SerializedObject):
    """Locate agent state that will be saved in json writeback file.

    This should be very small because the state is checkpointed frequently.
    """

    schema = [
        dict(name="client_id",
             doc="A unique identified for the client."),

        dict(name="last_flow_time", type="epoch",
             doc="The create timestamp of the last flow we processed."),

        dict(name="private_key", type=crypto.RSAPrivateKey,
             doc="The client's private key"),
    ]


class ClientPolicy(ExternalFileMixin,
                   serializer.SerializedObject):

    """The persistent state of the agent."""

    _writeback = obj.NoneObject("No writeback set")

    schema = [
        dict(name="manifest_location", type=location.Location,
             doc="The location of the installation manifest file. "
             "NOTE: This must be unauthenticated because it contains "
             "information required to initialize the connection."),

        dict(name="writeback_path",
             doc="Any persistent changes will be written to this location."),

        dict(name="labels", repeated=True,
             doc="A set of labels for this client."),
    ]

    @property
    def client_id(self):
        return self.writeback.client_id

    @classmethod
    def from_primitive(cls, data, session=None):
        """Automatically handle loading the writeback location.

        The client configuration can be merged with a local writeback
        object. Clients use this local file to store state.
        """
        result = super(ClientPolicy, cls).from_primitive(data, session=session)
        result.set_writeback(ClientWriteback(session=session))
        if result.writeback_path:
            try:
                session.logging.debug(
                    "Will load writeback from %s", result.writeback_path)
                with open(result.writeback_path, "rb") as fd:
                    result.set_writeback(ClientWriteback.from_primitive(
                        session=session, data=json.loads(fd.read())))
            except (IOError, TypeError, AttributeError):
                pass

        return result

    @property
    def writeback(self):
        return self._writeback

    def get_writeback(self):
        return self._writeback

    def set_writeback(self, value):
        self._writeback = value

    def save_writeback(self):
        self._session.logging.debug(
            "Updating writeback %s", self.writeback_path)
        with open(self.writeback_path, "wb") as fd:
            fd.write(self._writeback.to_json())


class ServerPolicy(ExternalFileMixin,
                   serializer.SerializedObject):
    """The configuration of all server side batch jobs.

    There are many ways to organize the agent's server side code. Although
    inherently the Rekall agent is all about tranferring files to the server,
    there has to be a systemic arrangement of where to store these files and how
    to deliver them (i.e. the Location object's specification).

    The final choice of Location objects is therefore implemented via the
    ServerPolicy object. Depending on the type of deployment, different
    parameters will be required, but ultimately the ServerPolicy object will be
    responsible to produce the required Location objects.

    This is the baseclass of all ServerPolicy objects.
    """

    schema = [
        dict(name="certificate", type=crypto.X509Ceritifcate,
             doc="The server's certificate"),

        dict(name="private_key", type=crypto.RSAPrivateKey,
             doc="The server's private key"),
    ]


class Configuration(ExternalFileMixin,
                    serializer.SerializedObject):
    """The agent configuration system.

    Both client side and server side configuration exist here, but on clients,
    the server side will be omitted.
    """

    schema = [
        dict(name="server", type=ServerPolicy,
             doc="The server's configuration."),

        dict(name="client", type=ClientPolicy,
             doc="The client's configuration."),

        dict(name="manifest", type=Manifest,
             doc="The installation manifest. Will be fetched from "
             "client.manifest_location"),

        dict(name="ca_certificate", type=crypto.X509Ceritifcate,
             doc="The certificate of the CA. Clients have this certificate "
             "hard coded and only trust data signed by it.")
    ]
