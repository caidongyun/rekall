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
from rekall_agent import action
from rekall_agent import result_collections


class CollectAction(action.Action):
    """Collect the results of an efilter query into a collection."""

    schema = [
        dict(name="query",
             doc="The dotty/EFILTER query to run."),

        dict(name="query_parameters", repeated=True, type="unicode",
             doc="Positional parameters for parametrized queries."),

        dict(name="collection", type=result_collections.CollectionSpec,
             doc="A specification for constructing the output collection."),
    ]

    def collect(self):
        """A row generator of collections."""
        # Insert data into the collection.
        for match in self._session.plugins.search(
                query=self.query,
                query_parameters=self.query_parameters).collect():
            yield match.ordered_dict

    def run(self):
        # Only a single table is supported in the collection spec.
        if len(self.collection.tables) != 1:
            raise TypeError("Only a single table is supported.")

        # Open the collection for writing.
        self.collection.open("w")
        for row in self.collect():
            self.collection.insert(row=row)

        # We are done.
        self.collection.close()

        return [self.collection]
