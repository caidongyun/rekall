# Rekall Memory Forensics
#
# Copyright 2015 Google Inc. All Rights Reserved.
#
# Authors:
# Copyright (C) 2015 Michael Cohen <scudette@google.com>
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

"""This Address Space allows us to open aff4 images.

AFF4 images are produced by the Rekall memory acquisition tools (Pmem and
friends).

For this address space to work:

pip install pyaff4

"""
import logging
import re
import os

from rekall import addrspace
from rekall import yaml_utils
from rekall import utils
from rekall.plugins.addrspaces import standard

from pyaff4 import data_store
from pyaff4 import zip
from pyaff4 import lexicon
from pyaff4 import rdfvalue

from pyaff4 import plugins  # pylint: disable=unused-import


# Control the logging level for the pyaff4 library logger.
LOGGER = logging.getLogger("pyaff4")
LOGGER.setLevel(logging.ERROR)


class AFF4StreamWrapper(object):
    def __init__(self, stream):
        self.stream = stream

    def read(self, offset, length):
        self.stream.seek(offset)
        return self.stream.read(length)

    def end(self):
        return self.stream.Size()

    def __unicode__(self):
        return utils.SmartUnicode(self.stream.urn)


class AFF4AddressSpace(addrspace.CachingAddressSpaceMixIn,
                       addrspace.MultiRunBasedAddressSpace):
    """Handle AFF4Map or AFF4Image type streams.

    Since AFF4 volumes may contain multiple streams, we allow the stream to be
    specified inside the volume path. For example suppose the volume located at:

    /home/mic/images/myimage.aff4

    Contains a stream called PhysicalMemory, then we can specify the filename
    as:

    /home/mic/images/myimage.aff4/PhysicalMemory

    If we just specified the path to the volume, then this address space will
    pick the first AFF4 stream which has an aff4:category of
    lexicon.AFF4_MEMORY_PHYSICAL.

    So if you have more than one physical memory stream in the same volume, you
    will need to specify the full path to the stream within the volume.
    """
    __name = "aff4"
    __image = True

    # This AS can map files into itself.
    __can_map_files = True

    order = standard.FileAddressSpace.order - 10

    def __init__(self, filename=None, **kwargs):
        super(AFF4AddressSpace, self).__init__(**kwargs)
        self.as_assert(self.base == None,
                       "Must stack on another address space")

        path = filename or self.session.GetParameter("filename")
        self.as_assert(path != None, "Filename must be specified")

        self.image = None
        self.phys_base = self
        self.resolver = data_store.MemoryDataStore()

        # A map between the filename and the offset it is mapped into the
        # address space.
        self.mapped_files = {}

        try:
            volume_path, stream_path = self._LocateAFF4Volume(path)
        except IOError:
            raise addrspace.ASAssertionError("Unable to open AFF4 volume")

        # filename is a volume, and there is no stream specified, just autoload
        # the stream if possible.
        if not stream_path:
            try:
                self._AutoLoadAFF4Volume(volume_path)
                return
            except IOError:
                raise addrspace.ASAssertionError("Unable to open AFF4 volume")

        # If the user asked for a specific stream just load that one. Note that
        # you can still load the pagefile manually using the --pagefile
        # parameter.
        with zip.ZipFile.NewZipFile(self.resolver, volume_path) as volume:
            self.volumes.add(volume.urn)
            self._LoadMemoryImage(volume.urn.Append(stream_path))

    def _LocateAFF4Volume(self, filename):
        stream_name = []
        volume_path = filename
        while volume_path:
            try:
                volume_urn = rdfvalue.URN.FromFileName(volume_path)
                with self.resolver.AFF4FactoryOpen(volume_urn) as aff4_stream:
                    if stream_name:
                        return aff4_stream.urn, os.path.join(*stream_name)

                    return aff4_stream.urn, None

            except IOError:
                volume_path, stream_component = os.path.split(volume_path)
                if not stream_component:
                    break

                stream_name.insert(0, stream_component)

        raise IOError("Not found")

    def _AutoLoadAFF4Volume(self, path):
        with zip.ZipFile.NewZipFile(self.resolver, path) as volume:
            self.volume_urn = volume.urn

            # We are searching for images with the physical memory category.
            for (subject, _, value) in self.resolver.QueryPredicate(
                    lexicon.AFF4_CATEGORY):
                if value == lexicon.AFF4_MEMORY_PHYSICAL:
                    self._LoadMemoryImage(subject)
                    break

        self.as_assert(self.image is not None,
                       "No physical memory categories found.")

        self.filenames = {}
        # Newer AFF4 images should have the AFF4_STREAM_ORIGINAL_FILENAME
        # attribute set.
        for (subject, _, value) in self.resolver.QueryPredicate(
                lexicon.AFF4_STREAM_ORIGINAL_FILENAME):
            # Normalize the filename for case insensitive filesysyems.
            self.filenames[unicode(value).lower()] = subject

        # TODO: Deprecate this guessing once all images have the
        # AFF4_STREAM_ORIGINAL_FILENAME attribute.
        for subject in self.resolver.QuerySubject(re.compile(".")):
            relative_name = self.volume_urn.RelativePath(subject)
            if relative_name:
                filename = self._normalize_filename(relative_name)
                self.filenames[filename] = subject

    def _normalize_filename(self, filename):
        """Normalize the filename based on the source OS."""
        m = re.match(r"/?([a-zA-Z]:[/\\].+)", filename)
        if m:
            # This is a windows filename.
            filename = m.group(1).replace("/", "\\")

            # The 32 bit WinPmem imager access native files via SysNative but
            # they are really located in System32.
            filename = filename.replace("SysNative", "System32")

            return filename.lower()

        return filename

    def _LoadMemoryImage(self, image_urn):
        aff4_stream = self.resolver.AFF4FactoryOpen(image_urn)
        self.image = AFF4StreamWrapper(aff4_stream)

        # Add the ranges if this is a map.
        try:
            for map_range in aff4_stream.GetRanges():
                self.runs.insert((map_range.map_offset,
                                  map_range.map_offset,
                                  map_range.length,
                                  self.image))
        except AttributeError:
            self.runs.insert((0, 0, aff4_stream.Size(), self.image))

        self.session.logging.info("Added %s as physical memory", image_urn)

    def ConfigureSession(self, session):
        self._parse_physical_memory_metadata(session, self.image.stream.urn)

    def file_mapping_offset(self, filename):
        """Returns the offset where the filename should be mapped.

        This function manages the session cache. By storing the file mappings in
        the session cache we can guarantee repeatable mappings.
        """
        mapped_files = self.session.GetParameter("file_mappings", {})
        if filename in mapped_files:
            return utils.CaseInsensitiveDictLookup(
                filename, mapped_files)

        # Give a bit of space for the mapping and page align it.
        mapped_offset = (self.end() + 0x10000) & 0xFFFFFFFFFFFFF000
        mapped_files[filename] = mapped_offset

        self.session.SetCache("file_mappings", mapped_files)

        return mapped_offset

    def get_mapped_offset(self, filename, file_offset):
        """Map the filename into the address space.

        If the filename is found in the AFF4 image, we return the offset at
        which it is mapped. Otherwise return None.
        """
        mapped_offset = None
        filename = self._normalize_filename(filename)
        mapped_offset = utils.CaseInsensitiveDictLookup(
            filename, self.mapped_files)
        if mapped_offset is None:
            # Try to map the file.
            subject = utils.CaseInsensitiveDictLookup(
                filename, self.filenames)

            if subject:
                stream = self.resolver.AFF4FactoryOpen(subject)
                mapped_offset = self.file_mapping_offset(filename)
                self.add_run(mapped_offset, 0, stream.Size(),
                             AFF4StreamWrapper(stream))

                self.session.logging.info(
                    "Mapped %s into address %#x", stream.urn, mapped_offset)

            else:
                # Cache failures too.
                mapped_offset = -1

        # Cache for next time.
        self.mapped_files[filename] = mapped_offset
        if mapped_offset > 0:
            return mapped_offset + file_offset

    def _parse_physical_memory_metadata(self, session, image_urn):
        try:
            with self.resolver.AFF4FactoryOpen(
                    image_urn.Append("information.yaml")) as fd:
                metadata = yaml_utils.decode(fd.read(10e6))
                # Allow the user to override the AFF4 file.
                if not session.HasParameter("dtb"):
                    session.SetCache(
                        "dtb", metadata.get("Registers", {}).get("CR3"),
                        volatile=False)

                if not session.HasParameter("kernel_base"):
                    session.SetCache(
                        "kernel_base", metadata.get("KernBase"),
                        volatile=False)
        except IOError:
            session.logging.warn(
                "AFF4 volume does not contain PhysicalMemory metadata.")

    def describe(self, address):
        try:
            virt_addr, file_address, _, stream_as = self.runs.find_le(
                address)

            # For normal physical memory addresses just be concise.
            if stream_as == self.image:
                return u"%#x" % address

            # For other mapped streams, just say which ones they are.
            return u"%#x @ %s (Mapped %#x)" % (
                address - virt_addr + file_address,
                stream_as, address)

        except ValueError:
            pass

        # For unmapped streams just say we have no idea.
        return u"%#x (Unmapped)" % address



# pylint: disable=unused-import
# Add these so that pyinstaller builds these dependencies in.
import rdflib.plugins.memory
import rdflib.plugins.parsers.hturtle
import rdflib.plugins.parsers.notation3
import rdflib.plugins.parsers.nquads
import rdflib.plugins.parsers.nt
import rdflib.plugins.parsers.rdfxml
import rdflib.plugins.parsers.structureddata
import rdflib.plugins.parsers.trig
import rdflib.plugins.parsers.trix
import rdflib.plugins.serializers.n3
import rdflib.plugins.serializers.nquads
import rdflib.plugins.serializers.nt
import rdflib.plugins.serializers.rdfxml
import rdflib.plugins.serializers.trig
import rdflib.plugins.serializers.trix
import rdflib.plugins.serializers.turtle
import rdflib.plugins.sleepycat
import rdflib.plugins.sparql.processor
import rdflib.plugins.sparql.results.csvresults
import rdflib.plugins.sparql.results.jsonresults
import rdflib.plugins.sparql.results.tsvresults
import rdflib.plugins.sparql.results.txtresults
import rdflib.plugins.sparql.results.xmlresults
import rdflib.plugins.stores.auditable
import rdflib.plugins.stores.concurrent
import rdflib.plugins.stores.sparqlstore