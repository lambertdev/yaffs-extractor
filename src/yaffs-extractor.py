#!/usr/bin/env python

import os
import sys
import struct
import string

class Compat(object):
    '''
    Python2/3 compatability methods.
    '''

    @staticmethod
    def str2bytes(s):
        if isinstance(s, str):
            return s.encode('latin-1')
        else:
            return s

    @staticmethod
    def iterator(d):
        if sys.version_info[0] > 2:
            return d.items()
        else:
            return d.iteritems()

    @staticmethod
    def has_key(d, k):
        if sys.version_info[0] > 2:
            return k in d
        else:
            return d.has_key(k)

class YAFFSException(Exception):
    pass

class YAFFSConfig(object):
    '''
    Container class for storing global configuration data.
    Also includes methods for automatic detection of the
    YAFFS configuration settings required for proper file
    system extraction.
    '''

    # These are signatures that identify the start of a spare data section,
    # and hence, the end of a page. If they can be identified, then we can
    # determine the page size. Take the following hexdump for example:
    #
    # 00000800  00 10 00 00 01 01 00 00  00 00 00 00 ff ff ff ff  |................|
    # 00000810  03 00 00 00 01 01 00 00  ff ff 62 61 72 00 00 00  |..........bar...|
    # 00000820  00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00  |................|
    #
    # The page ends (and spare data begins) at offset 0x800; note that it starts
    # with the bytes 0x00100000. These represent the object's chunk ID and are reliable
    # for the first YAFFS object entry. These would, of course, be byte swapped on
    # a big endian target. Further, if ECC was not used, there would be two additional
    # bytes (0xFFFF) in front of the 0x00100000, so these signatures can also be
    # used to detect if ECC is used or not.
    #
    # Note that this should work for a typical Linux YAFFS rootfs, but not for all
    # possible YAFFS file system images.
    SPARE_START_BIG_ENDIAN_ECC = b"\x00\x00\x10\x00"
    SPARE_START_BIG_ENDIAN_NO_ECC = b"\xFF\xFF\x00\x00\x10\x00"
    SPARE_START_LITTLE_ENDIAN_ECC = b"\x00\x10\x00\x00"
    SPARE_START_LITTLE_ENDIAN_NO_ECC = b"\xFF\xFF\x00\x10\x00\x00"

    def __init__(self, **kwargs):
        self.endianess = YAFFS.LITTLE_ENDIAN
        self.page_size = YAFFS.DEFAULT_PAGE_SIZE
        self.spare_size = YAFFS.DEFAULT_SPARE_SIZE
		self.block_size =  YAFFS.DEFAULT_BLOCK_SIZE
        self.ecclayout = True
        self.preserve_mode = True
        self.preserve_owner = False
        self.debug = False
        self.auto = False
        self.sample_data = None

        for (k, v) in Compat.iterator(kwargs):
            if v is not None:
                setattr(self, k, v)

        if self.auto and self.sample_data:
            self._auto_detect_settings()

    def print_settings(self):
        if self.endianess == YAFFS.LITTLE_ENDIAN:
            endian_str = "Little"
        else:
            endian_str = "Big"

        sys.stdout.write("Page size: %d\n" % self.page_size)
        sys.stdout.write("Spare size: %d\n" % self.spare_size)
        sys.stdout.write("ECC layout: %s\n" % self.ecclayout)
        sys.stdout.write("Endianess: %s\n\n" % endian_str)

    def _auto_detect_settings(self):
        '''
        This method attempts to identify the page size, spare size, and ECC configuration
        for the provided sample data. There are various methods of doing this, but here we
        rely on signature based detection. The other method I've seen used is to see if
        the file is an even multiple of the page size plus the spare size. This method
        usually assumes that the spare size is 1/32nd of the page size (it doesn't have
        to be), and also assumes that there is no trailing data on the end of the file
        system (there very well may be if it's been pulled from a firmware update or a
        live system).

        The signature method works even if assumptions about the relationship between
        page size and spare size are violated (in practice they are), and also if we are
        fed a file that has trailing garbage data. It also allows us to detect the ECC
        configuration, which is important if you want your YAFFS parsing to actually work.
        '''

        # Some tools assume that the spare size is 1/32nd of the page size.
        # For example, if your page size is 4096, then your spare size must be 128.
        # While this is the default for mkyaffs, you can mix and match, and in
        # practice, that is exactly what is seen in the wild.
        #
        # Thus, we keep a list of valid page sizes and spare sizes, but there
        # is no restriction on their pairing.
        valid_page_sizes = YAFFS.PAGE_SIZES + [-1]
        valid_spare_sizes = YAFFS.SPARE_SIZES

        # Spare data should start at the end of the page. Assuming that the page starts
        # at the beginning of the data blob we're working with (if it doesn't, nothing
        # is going to work correctly anyway), if we can identify where the spare data starts
        # then we know the page size.
        for page_size in valid_page_sizes:

            if page_size == -1:
                raise YAFFSException("Auto-detection failed: Could not locate start of spare data section.")

            # Matching the spare data signatures not only tells us the page size, but also
            # endianess and ECC layout as well!
            if self.sample_data[page_size:].startswith(self.SPARE_START_LITTLE_ENDIAN_ECC):
                self.page_size = page_size
                self.ecclayout = True
                self.endianess = YAFFS.LITTLE_ENDIAN
                break
            elif self.sample_data[page_size:].startswith(self.SPARE_START_LITTLE_ENDIAN_NO_ECC):
                self.page_size = page_size
                self.ecclayout = False
                self.endianess = YAFFS.LITTLE_ENDIAN
                break
            elif self.sample_data[page_size:].startswith(self.SPARE_START_BIG_ENDIAN_ECC):
                self.page_size = page_size
                self.ecclayout = True
                self.endianess = YAFFS.BIG_ENDIAN
                break
            elif self.sample_data[page_size:].startswith(self.SPARE_START_BIG_ENDIAN_NO_ECC):
                self.page_size = page_size
                self.ecclayout = False
                self.endianess = YAFFS.BIG_ENDIAN
                break

        # Now to try to identify the spare data size...
        try:
            # If not using the ECC layout, there are 2 extra bytes at the beginning of the
            # spare data block. Ignore them.
            if not self.ecclayout:
                offset = 6
            else:
                offset = 4

            # The spare data signature is built dynamically, as there are repeating data patterns
            # that we can match on to find where the spare data ends. Take this hexdump for example:
            #
            # 00000800  00 10 00 00 01 01 00 00  00 00 00 00 ff ff ff ff  |................|
            # 00000810  03 00 00 00 01 01 00 00  ff ff 62 61 72 00 00 00  |..........bar...|
            # 00000820  00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00  |................|
            #
            # The spare data starts at offset 0x800 and is 16 bytes in size. The next page data then
            # starts at offset 0x810. Not that the four bytes at 0x804 (in the spare data section) and
            # the four bytes at 0x814 (in the next page data section) are identical. This is because
            # the four bytes at offset 0x804 represent the object ID of the previous object, and the four
            # bytes at offset 0x814 represent the parent object ID of the next object. Also, the
            # four bytes in the page data are always followed by 0xFFFF, as those are the unused name
            # checksum bytes.
            #
            # Thus, the signature for identifying the next page section (and hence, the end of the
            # spare data section) becomes: [the 4 bytes starting at offset 0x804] + 0xFFFF
            #
            # Note that this requires at least one non-empty subdirectory; in practice, any Linux
            # file system should meet this requirement, but one could create a file system that
            # does not meet this requirement.
            spare_sig = self.sample_data[self.page_size+offset:self.page_size+offset+4] + b"\xFF\xFF"

            # Spare section ends 4 bytes before the spare_sig signature
            self.spare_size = self.sample_data[self.page_size:].index(spare_sig) - 4
        except Exception as e:
            raise YAFFSException("Auto-detection failed: Could not locate end of spare data section.")

        # Sanity check the spare size, make sure it looks legit
        if self.spare_size not in valid_spare_sizes:
            raise YAFFSException("Auto-detection failed: Detected an unlikely spare size: %d" % self.spare_size)

class YAFFS(object):
    '''
    Main YAFFS class; all other YAFFS classes are subclassed from this.
    It contains some basic definitions and methods used throughout the subclasses.
    '''

    BIG_ENDIAN = ">"
    LITTLE_ENDIAN = "<"

    # Valid page and spare sizes
    PAGE_SIZES  = [512, 1024, 2048, 4096, 8192, 16384]
    SPARE_SIZES = [16,  32,   64,   128,  256,  512]

    # These are the default values used by mkyaffs
    DEFAULT_PAGE_SIZE           = 2048
    DEFAULT_SPARE_SIZE          = 64
	DEFAULT_BLOCK_SIZE 			= 0
    
    # These assume non-unicode YAFFS name lengths
    # NOTE: In the YAFFS code YAFFS_MAX_NAME_LENGTH is #defined as 255.
    #       Although it does not say so, from observation this length
    #       must include the two (unused) name checksum bytes, and as
    #       such, it is defined here as 253.
    YAFFS_MAX_NAME_LENGTH       = 255 - 2
    YAFFS_MAX_ALIAS_LENGTH      = 159

    # Object type IDs
    YAFFS_OBJECT_TYPE_UNKNOWN   = 0
    YAFFS_OBJECT_TYPE_FILE      = 1
    YAFFS_OBJECT_TYPE_SYMLINK   = 2
    YAFFS_OBJECT_TYPE_DIRECTORY = 3
    YAFFS_OBJECT_TYPE_HARDLINK  = 4
    YAFFS_OBJECT_TYPE_SPECIAL   = 5
	
	YAFFS_OBJECT_ID_ROOT		= 1
	YAFFS_OBJECT_ID_LOSTNFOUND	= 2
	YAFFS_OBJECT_ID_UNLINKED	= 3
	YAFFS_OBJECT_ID_DELETED		= 4

	YAFFS_CHKPT_SEQ			= 0x21
    # These must be overidden with valid data by any subclass wishing
    # to use the read_long, read_short, read_next or read_block methods.
    #
    # data   - The data that the subclass needs to be read/parsed.
    # offset - This is initialized to zero and auto-incremented by the read_next method.
    #          Usually no need for subclasses to touch this unless they want to know how
    #          far into the data they've read so far.
    # config - An instance of the YAFFSConfig class.
    data = b''
    offset = 0
    config = None

    def dbg_write(self, msg):
        '''
        Prints debug message if self.config.debug is True.
        '''
        if self.config.debug:
            sys.stderr.write(msg)

    def read_long(self):
        '''
        Reads 4 bytes from the current self.offset location inside of self.data.
        Returns those 4 bytes as an integer.
        Endianess is determined by self.config.endianess.
        Does not increment self.offset.
        '''
        return struct.unpack("%sL" % self.config.endianess, self.data[self.offset:self.offset+4])[0]

    def read_short(self):
        '''
        Reads 2 bytes from the current self.offset location inside of self.data.
        Returns those 4 bytes as an integer.
        Endianess is determined by self.config.endianess.
        Does not increment self.offset.
        '''
        return struct.unpack("%sH" % self.config.endianess, self.data[self.offset:self.offset+2])[0]

    def read_next(self, size, raw=False):
        '''
        Reads the next size bytes from self.data and increments self.offset by size.
        If size is 2 or 4, by default self.read_long or self.read_short will be called respectively,
        unless raw is set to True.
        '''
        if size == 4 and not raw:
            val = self.read_long()
        elif size == 2 and not raw:
            val = self.read_short()
        else:
            val = self.data[self.offset:self.offset+size]

        self.offset += size
        return val

    def read_block(self):
        '''
        Reads the next page of data from self.data, including the spare OOB data.
        Returns a tuple of (page_data, spare_data).
        The page and spare data sizes are determined by self.config.page_size and
        self.config.spare_size.
        '''
        self.dbg_write("Reading page data from 0x%X - 0x%X\n" % (self.offset, self.offset+self.config.page_size))
        page_data = self.read_next(self.config.page_size)
            
        self.dbg_write("Reading spare data from 0x%X - 0x%X\n" % (self.offset, self.offset+self.config.spare_size))
        spare_data  = self.read_next(self.config.spare_size)

        return (page_data, spare_data)
		
	def proceed_block(self):
		'''
		Proceed one flash block to skip some special data
		'''
		self.dbg_write("Skip Block from 0x%X\n" % self.offset)
		self.offset += (self.config.block_size-1)*(self.config.spare_size+self.config.page_size)

    def null_terminate_string(self, string):
        '''
        Searches a string for the first null byte and terminates the
        string there. Returns the truncated string.
        '''
        try:
            i = string.index(b'\x00')
        except Exception as e:
            i = len(string)

        return string[0:i]

class YAFFSObjType(YAFFS):
    '''
    YAFFS object type container. The object type is just a 4 byte identifier.
    '''

    # Just maps object ID values to printable names, used by self.__str__
    TYPE2STR = {
                YAFFS.YAFFS_OBJECT_TYPE_UNKNOWN   : "YAFFS_OBJECT_TYPE_UNKNOWN",
                YAFFS.YAFFS_OBJECT_TYPE_FILE      : "YAFFS_OBJECT_TYPE_FILE",
                YAFFS.YAFFS_OBJECT_TYPE_SYMLINK   : "YAFFS_OBJECT_TYPE_SYMLINK",
                YAFFS.YAFFS_OBJECT_TYPE_DIRECTORY : "YAFFS_OBJECT_TYPE_DIRECTORY",
                YAFFS.YAFFS_OBJECT_TYPE_HARDLINK  : "YAFFS_OBJECT_TYPE_HARDLINK",
                YAFFS.YAFFS_OBJECT_TYPE_SPECIAL   : "YAFFS_OBJECT_TYPE_SPECIAL",
               }

    def __init__(self, data, config):
        '''
        data   - Raw 4 byte object type identifier data.
        config - An instance of YAFFSConfig.
        '''
        self.data = data
        self.config = config
        self._type = self.read_next(4)

        if self._type not in self.TYPE2STR.keys():
            raise YAFFSException("Invalid object type identifier: 0x%X!" % self._type)

    def __str__(self):
        return self.TYPE2STR[self._type]

    def __int__(self):
        return self._type

    def __get__(self, instance, owner):
        return self._type

class YAFFSSpare(YAFFS):
    '''
    Parses and stores relevant data from YAFFS spare data sections.
    Primarily important for retrieving each file object's ID.
    '''

    def __init__(self, data, config):
        '''
        data   - Raw bytes of the spare OOB data.
        config - An instance of YAFFSConfig.
        '''
        self.data = data
        self.config = config

        # YAFFS images built without --yaffs-ecclayout have an extra two
        # bytes before the chunk ID. Possibly an unused CRC?
        #if not self.config.ecclayout:
        #    junk = self.read_next(2)
		
		self.seq_number = self.read_next(4)
		self.obj_id = self.read_next(4)
        self.chunk_id = self.read_next(4)
		self.n_bytes = self.read_next(4)
		

class YAFFSEntry(YAFFS):
    '''
    Parses and stores information from each YAFFS object entry data structure.
    TODO: Implement as a ctypes Structure class?
    '''

    def __init__(self, data, spare, config):
        '''
        data   - Page data, as returned by YAFFS.read_block.
        spare  - Spare OOB data, as returned by YAFFS.read_block.
        config - An instance of YAFFSConfig.
        '''
        self.data = data
        self.config = config
        # This is filled in later, by YAFFSParser.next_entry
        self.file_data = b''

        # Read in the first four bytes, which are the object type ID,
        # and pass them to YAFFSObjType for processing.
        obj_type_raw = self.read_next(4, raw=True)
        self.yaffs_obj_type = YAFFSObjType(obj_type_raw, self.config)

        # The object ID of this object's parent (e.g., the ID of the directory
        # that a file resides in).
        self.parent_obj_id = self.read_next(4)

        # File name and checksum (checksum no longer used in YAFFS)
        self.sum_no_longer_used = self.read_next(2)
        self.name = self.null_terminate_string(self.read_next(self.YAFFS_MAX_NAME_LENGTH+1))

        # Should be 0xFFFFFFFF
        junk = self.read_next(4)

        # File mode and ownership info
        self.yst_mode = self.read_next(4)
        self.yst_uid = self.read_next(4)
        self.yst_gid = self.read_next(4)

        # File timestamp info
        self.yst_atime = self.read_next(4)
        self.yst_mtime = self.read_next(4)
        self.yst_ctime = self.read_next(4)

        # Low 32 bits of file size
        self.file_size_low = self.read_next(4)

        # Used for hard links, specifies the object ID of the file to be hardlinked to.
        self.equiv_id = self.read_next(4)

        # Aliases are for symlinks only
        self.alias = self.null_terminate_string(self.read_next(self.YAFFS_MAX_ALIAS_LENGTH+1))

        # Stuff for block and char devices (equivalent of stat.st_rdev in C)
        self.yst_rdev = self.read_next(4)

        # Appears to be for timestamp stuff for WinCE
        self.win_ctime_1 = self.read_next(4)
        self.win_ctime_2 = self.read_next(4)
        self.win_atime_1 = self.read_next(4)
        self.win_atime_2 = self.read_next(4)
        self.win_mtime_1 = self.read_next(4)
        self.win_mtime_2 = self.read_next(4)

        # The only thing this code uses from these entries is file_size_high (high 32 bits of
        # the file size).
        self.inband_shadowed_obj_id = self.read_next(4)
        self.inband_is_shrink = self.read_next(4)
        self.file_size_high = self.read_next(4)
        self.reserved = self.read_next(1)
        self.shadows_obj = self.read_next(4)
        self.is_shrink = self.read_next(4)

        # Calculate file size from file_size_low and file_size_high.
        # Both will be 0xFFFFFFFF if unused.
        if self.file_size_high != 0xFFFFFFFF:
            self.file_size = self.file_size_low | (self.file_size_high << 32)
        elif self.file_size_low != 0xFFFFFFFF:
            self.file_size = self.file_size_low
        else:
            self.file_size = 0

        # Pass the spare data to YAFFSSpare for processing.
        # Keep a copy of this object's ID, as parsed from the spare data, for convenience.
        # self.spare = YAFFSSpare(spare, self.config)
        # self.yaffs_obj_id = self.spare.obj_id

class YAFFSExtractor(YAFFS):
    '''
    Class for extracting information and data from a YAFFS file system.
    '''

    def __init__(self, data, config):
        '''
        data   - Raw string containing YAFFS file system data.
                 Trailing data is usually OK, but the first byte
                 in data must be the beginning of the file system.
        config - An instance of YAFFSConfig.
        '''
        self.sum_n_used = 0
		self.sum_bad_block = 0
		self.sum_chkpt_block = 0
		
		self.file_paths = {}
        self.file_entries = {}
		self.file_chunks = {} #array for chunk id
		self.file_seq = {}
		
        self.data = data
        self.config = config

    def parse(self):
        '''
        Parses the YAFFS file system, builds directory structures and stores file info / data.
        Must be called before all other methods in this class.
        '''
		nand_chunk_id = 0

		while self.offset < self.data_len:
			(obj_hdr_data, obj_hdr_spare) = self.read_block()
			
			spare = YAFFSSpare(obj_hdr_spare, self.config)
			#A starting chunk
			if spare.seq_number = YAFFS.YAFFS_CHKPT_SEQ:
				if self.config.block_size != YAFFS.DEFAULT_BLOCK_SIZE:
					nand_chunk_id += self.config.block_size-1
					self.proceed_block()
				continue
			elif (self.file_chunks[spare.obj_id] is None) or (self.file_chunks[spare.obj_id]["chunks"][spare.chunk_id] is None) or (self.file_chunks[spare.obj_id]["chunks"][spare.chunk_id]["seq"] < spare.seq_number):
				self.file_chunks[spare.obj_id]["chunks"][spare.chunk_id]["seq"] = spare.seq_number
				self.file_chunks[spare.obj_id]["chunks"][spare.chunk_id]["nand_chunk_id"] = nand_chunk_id
				if spare.obj_id == 0:
					entry = YAFFSEntry(obj_hdr_data, obj_hdr_spare, self.config)
					self.file_entries[spare.obj_id] = entry
					self.file_chunks[parent_obj_id]["children"][spare.obj_id] = spare.obj_id
			else: 
				continue
			
			nand_chunk_id += 1;
		
    def _print_entry(self, entry):
        '''
        Prints info about a specific file entry.
        '''
        sys.stdout.write("###################################################\n")
        sys.stdout.write("File type: %s\n" % str(entry.yaffs_obj_type))
        sys.stdout.write("File ID: %d\n" % entry.yaffs_obj_id)
        sys.stdout.write("File parent ID: %d\n" % entry.parent_obj_id)
        sys.stdout.write("File name: %s" % self.file_paths[entry.yaffs_obj_id])
        if int(entry.yaffs_obj_type) == self.YAFFS_OBJECT_TYPE_SYMLINK:
            sys.stdout.write(" -> %s\n" % entry.alias)
        elif int(entry.yaffs_obj_type) == self.YAFFS_OBJECT_TYPE_HARDLINK:
            sys.stdout.write("\nPoints to file ID: %d\n" % entry.equiv_id)
        else:
            sys.stdout.write("\n")
        sys.stdout.write("File size: 0x%X\n" % entry.file_size)
        sys.stdout.write("File mode: %d\n" % entry.yst_mode)
        sys.stdout.write("File UID: %d\n" % entry.yst_uid)
        sys.stdout.write("File GID: %d\n" % entry.yst_gid)
        #sys.stdout.write("First bytes: %s\n" % entry.file_data[0:16])
        sys.stdout.write("###################################################\n\n")


    def ls(self):
        '''
        List info for all files in self.file_entries.
        '''
        sys.stdout.write("\n")
        for (entry_id, entry) in Compat.iterator(self.file_entries):
            self._print_entry(entry)

    def _set_mode_owner(self, file_path, entry):
        '''
        Conveniece wrapper for setting ownership and file permissions.
        '''
        if self.config.preserve_mode:
            os.chmod(file_path, entry.yst_mode)
        if self.config.preserve_owner:
            os.chown(file_path, entry.yst_uid, entry.yst_gid)
	
	def fix_file_path_iter(self, obj_id):
		for obj self.file_chunks[obj_id]["children"]:
			self.file_paths[obj] = os.path.join(self.file_paths[obj_id], self.file_paths[obj])
			entry = self.file_entries[obj]
            if int(entry.yaffs_obj_type) == self.YAFFS_OBJECT_TYPE_DIRECTORY:
				self.fix_file_path(obj)
	
	def fix_file_path(self):
		outdir = Compat.str2bytes(outdir)
		self.file_paths[YAFFS.YAFFS_OBJECT_ID_ROOT] = os.path.join(outdir, "")
		self.file_paths[YAFFS.YAFFS_OBJECT_ID_LOSTNFOUND] = os.path.join(outdir, "lost_n_found")
		self.file_paths[YAFFS.YAFFS_OBJECT_ID_UNLINKED] = os.path.join(outdir, "unlinked")
		self.file_paths[YAFFS.YAFFS_OBJECT_ID_DELETED] = os.path.join(outdir,"deleted")

		for i in range(YAFFS.YAFFS_OBJECT_ID_ROOT,YAFFS.YAFFS_OBJECT_ID_DELETED+1):
			self.fix_file_path(i)
			
		
    def extract(self, outdir):
        '''
        Creates the outdir directory and extracts all files there.
        '''
        dir_count = 0
        file_count = 0
        link_count = 0

        # Make it a bytes array for Python3

        # Create directories first, so that files can be written to them
        for (entry_id, file_path) in Compat.iterator(self.file_paths):
            entry = self.file_entries[entry_id]
            if file_path and int(entry.yaffs_obj_type) == self.YAFFS_OBJECT_TYPE_DIRECTORY:
                # Check the file name for possible path traversal attacks
                if b'..' in file_path:
                    sys.stderr.write("Warning: Refusing to create directory '%s': possible path traversal\n" % file_path)
                    continue

                try:
                    os.makedirs(file_path)
                    self._set_mode_owner(file_path, entry)
                    dir_count += 1
                except Exception as e:
                    sys.stderr.write("WARNING: Failed to create directory '%s': %s\n" % (file_path, str(e)))

        # Create files, including special device files
        for (entry_id, file_path) in Compat.iterator(self.file_paths):
            if file_path:
                # Check the file name for possible path traversal attacks
                if b'..' in file_path:
                    sys.stderr.write("Warning: Refusing to create file '%s': possible path traversal\n" % file_path)
                    continue

                entry = self.file_entries[entry_id]
                if int(entry.yaffs_obj_type) == self.YAFFS_OBJECT_TYPE_FILE:
                    try:
                        with open(file_path, 'wb') as fp:
							file_chunks = self.file_chunks[entry_id]["chunks"]
							for nand_chunk in file_chunks:
								nand_chunk_id = nand_chunk["nand_chunk_id"]
								if nand_chunk_id != 0:
									chunk_size = self.config.page_size+self.config.spare_size
									fp.write(data[nand_chunk_id*chunk_size:nand_chunk_id*chunk_size+self.config.page_size])
                        self._set_mode_owner(file_path, entry)
                        file_count += 1
                    except Exception as e:
                        sys.stderr.write("WARNING: Failed to create file '%s': %s\n" % (file_path, str(e)))
                elif int(entry.yaffs_obj_type) == self.YAFFS_OBJECT_TYPE_SPECIAL:
                    try:
                        os.mknod(file_path, entry.yst_mode, entry.yst_rdev)
                        file_count += 1
                    except Exception as e:
                        sys.stderr.write("Failed to create special device file '%s': %s\n" % (file_path, str(e)))


        # Create hard/sym links
        for (entry_id, file_path) in Compat.iterator(self.file_paths):
            entry = self.file_entries[entry_id]

            if file_path:
                # Check the file name for possible path traversal attacks
                if b'..' in file_path:
                    sys.stderr.write("Warning: Refusing to create link file '%s': possible path traversal\n" % file_path)
                    continue

                dst = file_path

                if int(entry.yaffs_obj_type) == self.YAFFS_OBJECT_TYPE_SYMLINK:
                    src = entry.alias
                    try:
                        os.symlink(src, dst)
                        link_count += 1
                    except Exception as e:
                        sys.stderr.write("WARNING: Failed to create symlink '%s' -> '%s': %s\n" % (dst, src, str(e)))
                elif int(entry.yaffs_obj_type) == self.YAFFS_OBJECT_TYPE_HARDLINK:
                    src =self.file_paths[entry.equiv_id]
                    try:
                        os.link(src, dst)
                        link_count += 1
                    except Exception as e:
                        sys.stderr.write("WARNING: Failed to create hard link '%s' -> '%s': %s\n" % (dst, src, str(e)))

        return (dir_count, file_count, link_count)


def parse_yaffs(fs):
    '''
    Attempts to parse the file system via the provided YAFFSExtractor instance.
    Returns True if no errors were encountered during process, else returns False.
    '''
    success = True

    # If something is going to go wrong, it will most likely be during the parsing stage
    try:
        fs.dbg_write("Parsing YAFFS objects...\n")
        fs.parse()
        fs.dbg_write("Parsed %d objects\n" % len(fs.file_entries))
    except Exception as e:
        fs.dbg_write("File system parsing failed: %s\n" % str(e))
        success = False

    return success

def main():
    from getopt import GetoptError, getopt

    page_size = None
    spare_size = None
	block_size = None
    endianess = None
    ecclayout = None
    preserve_mode = None
    preserve_owner = None
    debug = None
    auto_detect = None
    in_file = None
    out_dir = None
    list_files = False
    brute_force = False
    fs = None
    config = None

    try:
        (opts, args) = getopt(sys.argv[1:], "f:d:p:s:B:e:c:oaDlb", ["file=",
                                                                  "dir=",
                                                                  "page-size=",
                                                                  "spare-size=",
																  "block-size="
                                                                  "endianess=",
                                                                  "no-ecc",
                                                                  "ownership",
                                                                  "debug",
                                                                  "ls",
                                                                  "brute-force",
                                                                  "auto"])
    except GetoptError as e:
        sys.stderr.write(str(e) + "\n")
        sys.stderr.write("\nUsage: %s [OPTIONS]\n\n" % sys.argv[0])
        sys.stderr.write("    -f, --file=<yaffs image>        YAFFS input file *\n")
        sys.stderr.write("    -d, --dir=<output directory>    Extract YAFFS files to this directory **\n")
        sys.stderr.write("    -p, --page-size=<int>           YAFFS page size [default: 2048]\n")
        sys.stderr.write("    -s, --spare-size=<int>          YAFFS spare size [default: 64]\n")
		sys.stderr.write("    -B, --block-size=<int>          YAFFS block size(#pages per block) [default: 64]\n")
        sys.stderr.write("    -e, --endianess=<big|little>    Set input file endianess [default: little]\n")
        sys.stderr.write("    -n, --no-ecc                    Don't use the YAFFS oob scheme [default: use the oob scheme]\n")
        sys.stderr.write("    -a, --auto                      Attempt to auto detect page size, spare size, ECC, and endianess settings [default: False]\n")
        sys.stderr.write("    -b, --brute-force               Attempt all combinations of page size, spare size, ECC, and endianess  [default: False]\n")
        sys.stderr.write("    -o, --ownership                 Preserve original ownership of extracted files [default: False]\n")
        sys.stderr.write("    -l, --ls                        List file system contents [default: False]\n")
        sys.stderr.write("    -D, --debug                     Enable verbose debug output [default: False]\n\n")
        sys.stderr.write("*  = Required argument\n")
        sys.stderr.write("** = Required argument, unless --ls is specified\n\n")
        sys.exit(1)

    for (opt, arg) in opts:
        if opt in ["-f", "--file"]:
            in_file = arg
        elif opt in ["-d", "--dir"]:
            out_dir = arg
        elif opt in["-l", "--ls"]:
            list_files = True
        elif opt in ["-a", "--auto"]:
            auto_detect = True
        elif opt in ["-b", "--brute-force"]:
            brute_force = True
        elif opt in ["-n", "--no-ecc"]:
            ecclayout = False
        elif opt in ["-e", "--endianess"]:
            if arg.lower()[0] == 'b':
                endianess = YAFFS.BIG_ENDIAN
            else:
                endianess = YAFFS.LITTLE_ENDIAN
        elif opt in ["-s", "--spare-size"]:
            spare_size = int(arg)
        elif opt in ["-p", "--page-size"]:
            page_size = int(arg)
		elif opt in ["-B", "--block_size"]:
			block_size = int(arg)
        elif opt in ["-o", "--ownership"]:
            preserve_ownership = True
        elif opt in ["-D", "--debug"]:
            debug = True

    if not in_file or (not out_dir and not list_files):
        sys.stderr.write("Error: Missing required arguments! Try --help.\n")
        sys.exit(1)

    if out_dir:
        try:
            os.makedirs(out_dir)
        except Exception as e:
            sys.stderr.write("Failed to create output directory: %s\n" % str(e))
            sys.exit(1)

    try:
        with open(in_file, 'rb') as fp:
            data = fp.read()
    except Exception as e:
        sys.stderr.write("Failed to open file '%s': %s\n" % (in_file, str(e)))
        sys.exit(1)

    if auto_detect:
        try:
            # First 10K of data should b more than enough to detect the YAFFS settings
            config = YAFFSConfig(auto=True,
								 block_size = block_size,
                                 sample_data=data[0:10240],
                                 preserve_mode=preserve_mode,
                                 preserve_owner=preserve_owner,
                                 debug=debug)
        except YAFFSException as e:
            sys.stderr.write(str(e) + "\n")
            config = None

    if config is None:
        config = YAFFSConfig(page_size=page_size,
							 block_size=block_size,
                             spare_size=spare_size,
                             endianess=endianess,
                             ecclayout=ecclayout,
                             preserve_mode=preserve_mode,
                             preserve_owner=preserve_owner,
                             debug=debug)

    # Try auto-detected / manual / default settings first.
    # If those work without errors, then assume they are correct.
    fs = YAFFSExtractor(data, config)
    # If there were errors in parse_yaffs, and brute forcing is enabled, loop
    # through all possible configuration combinations looking for the one
    # combination that produces the most successfully parsed object entries.
    if not parse_yaffs(fs) and brute_force:
        for endianess in [YAFFS.LITTLE_ENDIAN, YAFFS.BIG_ENDIAN]:
            for ecclayout in [True, False]:
                for page_size in YAFFS.PAGE_SIZES:
                    for spare_size in YAFFS.SPARE_SIZES:
          
                        # This wouldn't make sense...
                        if spare_size > page_size:
                            continue

                        config = YAFFSConfig(page_size=page_size,
                                             spare_size=spare_size,
											 block_size=block_size,
                                             endianess=endianess,
                                             ecclayout=ecclayout,
                                             preserve_mode=preserve_mode,
                                             preserve_owner=preserve_owner,
                                             debug=debug)

                        tmp_fs = YAFFSExtractor(data, config)
                        parse_yaffs(tmp_fs)
                        if len(tmp_fs.file_entries) > len(fs.file_entries):
                            fs = tmp_fs

    if fs is None:
        sys.stdout.write("File system parsing failed, quitting...\n")
        return 1
    else:
        sys.stdout.write("Found %d file objects with the following YAFFS settings:\n" % len(fs.file_entries))
        fs.config.print_settings()

    if list_files:
        fs.ls()

    if out_dir:
        sys.stdout.write("Extracting file objects...\n")
        (dc, fc, lc) = fs.extract(out_dir)
        sys.stdout.write("Created %d directories, %d files, and %d links.\n" % (dc, fc, lc))

    return 0

if __name__ == "__main__":
    sys.exit(main())

