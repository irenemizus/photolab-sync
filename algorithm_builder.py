"""Operation builder for the SyncClient engine (see docs/design-algorithm.md).

This module is part of the **SyncClient** (the smart side). Given the local
structure and the remote structure collected from SyncServer, it produces the
complete, **ordered** list of atomic operations that SyncServer (a dumb
translator) must apply to make the remote side match the local side.

A "structure" is a mapping keyed by the photo's **content hash** (SHA512 over
the decoded *pixel data* only — metadata is NOT part of the hash) whose value
describes where that photo lives. See docs/design-algorithm.md §1 for the full
record model and the discussion of why the current hash->path form is only a
simplification.

Design constraints the produced plan must satisfy:
  * any managed-metadata change is a MOVE (even when the album is unchanged);
  * the ordering is safe to re-apply (idempotent) so a crashed run is completed
    by simply re-running the sync;
  * remote-only photos are deleted first (the local tree is the ideal).

Behavioral items marked [TODO] below are specified in docs/design-algorithm.md
but not yet implemented (this file is the seed, to be extended).
"""
import os
from enum import Enum
import Levenshtein

class OperationType(Enum):
    UPLOAD = 1
    DELETE = 2
    MOVE = 3
    CREATE_ALBUM = 4
    DELETE_ALBUM = 5
    RENAME_ALBUM = 6

class Operation:
    """One atomic operation SyncServer must apply.

    MOVE semantics (docs/design-algorithm.md §4, docs/design-api.md §6.3): a
    MOVE is emitted whenever a photo's album changes **or** its metadata
    changes (same pixels). When only metadata changed, the remote and local
    "paths" are identical, so the current path-based detection below misses it
    and the MOVE payload must carry a refreshed file. See [TODO] in
    build_sync_algorithm.
    """

    def __init__(self, operation_type, remote_path, local_path=None):
        self.operation_type = operation_type
        self.local_path = local_path
        self.remote_path = remote_path

    def __str__(self):
        if self.operation_type == OperationType.UPLOAD:
            return f"UPLOAD: {self.remote_path}"
        elif self.operation_type == OperationType.DELETE:
            return f"DELETE: {self.remote_path}"
        elif self.operation_type == OperationType.MOVE:
            return f"MOVE: {self.remote_path} -> {self.local_path}"
        elif self.operation_type == OperationType.CREATE_ALBUM:
            return f"CREATE_ALBUM: {self.remote_path}"
        elif self.operation_type == OperationType.DELETE_ALBUM:
            return f"DELETE_ALBUM: {self.remote_path}"
        elif self.operation_type == OperationType.RENAME_ALBUM:
            return f"RENAME_ALBUM: {self.remote_path} -> {self.local_path}"
        else:
            return None


# This structure contains a tree of pictures in the following format:
# {
#     ("event1", "subevent1"): (
#         "1a8b53...": "filename1.jpg",
#         "c9d8e7...": "filename2.jpg"
#     ),
#     ("event2", "subevent2"): (
#         "qwe876...": "filename3.jpg",
#         "a4s5d6...": "filename4.jpg"
#     )
# }

class AlbumHashNames:
    # Constructs from a dictionary where the key is hash and value is filename
    # (a 7-level path). Albums are keyed by the 4-level prefix
    # year/month/event/subevent, so two folders sharing the same event-subevent
    # name but different timing already become distinct keys here.
    #
    # [TODO] Per docs/design-algorithm.md §3.3 this is not the whole story:
    #   * same-name albums are only unambiguous once matched against Immich
    #     albums (which store a name, not a path) via inferred year/month;
    #   * the "new year" case (Case B) must MERGE two boundary-adjacent folders
    #     into a single logical album before this grouping runs.
    def __init__(self, pairs):
        self.__data = dict()
        for hash in pairs.keys():
            year = pairs[hash].split("/")[0]
            month = pairs[hash].split("/")[1]
            event_name = pairs[hash].split("/")[2]
            subevent_name = pairs[hash].split("/")[3]
            key = os.path.join(year, month, event_name, subevent_name)
            if key not in self.__data:
                self.__data[key] = set()

            self.__data[key].add((hash, pairs[hash]))

    def get_event_names(self):
        return list(self.__data.keys())

    def get_pairs_for(self, event_name):
        return self.__data[event_name]


def extract_album_path(image_path: str):
    s = image_path.split("/")
    return s[0] + "/" + s[1] + "/" + s[2] + "/" + s[3]

def extract_album_name(image_path: str):
    s = image_path.split("/")
    return s[2] + " — " + s[3]

# Input format: each structure is a dict() keyed by the photo's content hash,
# whose value is the 7-level path to the file relative to the photolab root.
# Item example:
#   'fef07ef14141b06370c1f37dd8ad5152f62bafe8f8e435f1ef181c7c82ca1a0618462a7716ba3d82618d92049528606eaf5b36ce48888e2791cd60781c967f2a': '2026/April/Misc/Джаз Sandia Quartet в Бабе-Яге/General/Personal/3 stars/0L5A0599_1.jpg'
def build_sync_algorithm(local_pairs, remote_pairs):
    """Build the ordered list of atomic operations for one sync run.

    local_pairs / remote_pairs: hash -> 7-level path (see module docstring).
    Returns operations ordered as: DELETE, RENAME_ALBUM, CREATE_ALBUM, MOVE,
    DELETE_ALBUM, UPLOAD (docs/design-algorithm.md §6.1).

    [TODO] The full design (docs/design-algorithm.md) is not yet implemented
    here; the gaps versus the spec are:
      * value should be a record (place + full date_time), not just a path, so
        that a metadata-only change (e.g. day/time within the same month) is
        detected and emitted as a MOVE;
      * pre-sync validation: a single pixel hash appearing in more than one
        local event is a fatal error raised BEFORE any op is returned;
      * duplicate remote content (same hash, multiple assets) -> keep one,
        DELETE the rest;
      * empty local album -> ignored + logged; empty remote album -> kept +
        logged;
      * remote image with no event whose hash is local -> MOVE into its album;
      * same-name album collision: two local folders sharing event-subevent are
        either two split albums (Case A) or one "new year" merged album (Case B,
        contiguous across a year boundary); matching uses inferred year/month
        and albums are addressed by id;
      * the plan must be idempotent / re-runnable so a crashed sync completes
        by re-running.
    """
    remaining_local_pairs = local_pairs.copy()
    remaining_remote_pairs = remote_pairs.copy()

    # Collecting the albums of all the local hashes
    local_albums_pics = AlbumHashNames(local_pairs)
    local_event_names = local_albums_pics.get_event_names()

    # Collecting the albums of all the remote hashes
    remote_albums_pics = AlbumHashNames(remote_pairs)
    remote_event_names = remote_albums_pics.get_event_names()

    # Step 1: Finding new files
    # Check what hashes exist in local_hashes, but not in remote_hashes
    new_local_hashes = remaining_local_pairs.keys() - remaining_remote_pairs.keys()

    print(f"New hashes count: {len(new_local_hashes)}")


    # Adding UPLOAD operation for each new file
    uploading_operations = []
    for new_local_hash in new_local_hashes:
        uploading_operations.append(Operation(OperationType.UPLOAD, remaining_local_pairs[new_local_hash]))

    # Erasing all new_local_hashes from local_hashes
    for new_hash in new_local_hashes:
        remaining_local_pairs.pop(new_hash)

    # Step 2: Finding deleted files
    # Check what hashes exist in remote_hashes, but don't exist in local_hashes
    deleted_remote_hashes = remaining_remote_pairs.keys() - remaining_local_pairs.keys()
    print(f"Deleted hashes count: {len(deleted_remote_hashes)}")

    # Adding DELETE operation for each deleted old file
    deletion_operations = []
    for deleted_remote_hash in deleted_remote_hashes:
        deletion_operations.append(Operation(OperationType.DELETE, remaining_remote_pairs[deleted_remote_hash]))

    # Erasing all the deleted_remote_hashes from remote_hashes
    for deleted_remote_hash in deleted_remote_hashes:
        remaining_remote_pairs.pop(deleted_remote_hash)

    # Step 3a: Finding moved files (the files that exist in both local_hashes and remote_hashes, but have different path)
    # [TODO] This detects a move only when the *path* (and thus the album or
    # category/supplemental/rating/year/month) changes. The design requires a
    # MOVE for ANY metadata change, including a date-time (day/time) change
    # within the same month where the 7-level path is unchanged. Once the value
    # is a full record (place + date_time), compare the whole record here.
    moved_hashes = []
    assert(len(remaining_local_pairs) == len(remaining_remote_pairs))
    for local_hash in remaining_local_pairs:
        if remaining_remote_pairs[local_hash] != remaining_local_pairs[local_hash]:
            moved_hashes.append(local_hash)

    # Collecting remote albums that are missing in local
    remotes_missing_in_local = set()
    for remote_event in remote_event_names:
        found = False
        for local_event in local_event_names:
            if local_event == remote_event:
                found = True
        if not found:
            remotes_missing_in_local.add(remote_event)

    # Reverse index (hash -> local album) so that, for a remote album's pictures,
    # the local album they ended up in can be looked up in O(1) instead of
    # rescanning every local album's pictures for each one
    local_album_by_hash = {hash: extract_album_path(path) for hash, path in local_pairs.items()}

    # For each remote missing in local, collecting list of local albums
    # containing former pictures from this remote album, and rating every
    # such local album as a possible new name according to Levenshtein ratio.
    # A local album is a rename candidate only if its name is free on remote:
    # renaming into an already existing album would collide with it
    # TODO(design-algorithm.md): albums are now keyed by the 4-level identity
    # year/month/event/subevent, but a rename only changes the event/subevent part.
    # Re-derive the matching to compare the renameable (event/subevent, i.e. the display
    # name) while still keying by the full identity; comparing the full 4-level path (as
    # now) would treat a pure year/month change as a rename. Also handle the
    # same event/subevent with different timing ("split events" vs "New Year" single
    # event) and multiple same-named Immich albums.
    rename_candidates = []
    for missing_in_local in sorted(remotes_missing_in_local):
        local_events_containing_images_from_missing_remote = set()
        for pair_from_missing in remote_albums_pics.get_pairs_for(missing_in_local):
            local_event = local_album_by_hash.get(pair_from_missing[0])
            if local_event is not None:
                local_events_containing_images_from_missing_remote.add(local_event)

        for successor_album in local_events_containing_images_from_missing_remote:
            if successor_album in remote_event_names:
                continue
            rename_candidates.append((Levenshtein.ratio(successor_album, missing_in_local),
                                      missing_in_local,
                                      successor_album))

    # Picking the renames greedily, the best ratio first, so that no album is
    # renamed twice and no two albums are renamed into the same target name.
    # The names are a part of the sorting key to keep the result stable
    rename_candidates.sort(key=lambda candidate: (-candidate[0], candidate[1], candidate[2]))
    album_renames = dict()  # remote album name -> new (local) album name
    taken_target_names = set()
    for lratio, source_album, target_album in rename_candidates:
        if source_album in album_renames or target_album in taken_target_names:
            continue
        album_renames[source_album] = target_album
        taken_target_names.add(target_album)

    album_rename_operations = []
    for source_album in album_renames:
        album_rename_operations.append(Operation(OperationType.RENAME_ALBUM, source_album, album_renames[source_album]))

    # The remote albums missing in local that got no free successor name are deleted.
    # Their surviving pictures are moved out one by one before the deletion
    album_deletion_operations = []
    for missing_in_local in sorted(remotes_missing_in_local):
        if missing_in_local not in album_renames:
            album_deletion_operations.append(Operation(OperationType.DELETE_ALBUM, missing_in_local))

    renamed_albums = list(album_renames.values())

    # Adding single file movement operations for the files that are moved one by one, not by renaming the album.
    # The album renames are applied to the remote paths first, since the renames are executed before the movements
    movement_operations = []
    for hash_to_move in moved_hashes:
        remote_path = remaining_remote_pairs[hash_to_move]
        local_path = remaining_local_pairs[hash_to_move]
        remote_album = extract_album_path(remote_path)
        if remote_album in album_renames:
            remote_path = album_renames[remote_album] + remote_path[len(remote_album):]
        if remote_path != local_path:
            movement_operations.append(Operation(OperationType.MOVE, remote_path, local_path))


    # Adding CREATE_ALBUM operation for each new file's album that is missing on remote and wasn't renamed
    album_creation_operations = []
    created_albums = set()
    for local_hash in local_pairs.keys():
        local_image_path = local_pairs[local_hash]
        local_image_album = extract_album_path(local_image_path)
        if not local_image_album in renamed_albums and not local_image_album in created_albums:
            if local_image_album in local_albums_pics.get_event_names():
                found = False
                for remote_album in remote_albums_pics.get_event_names():
                    if local_image_album == remote_album:
                        found = True
                        break
                if not found:
                    album_creation_operations.append(Operation(OperationType.CREATE_ALBUM, local_image_album))
                    created_albums.add(local_image_album)


    # The order matters: the albums are renamed and created before the files are moved into them,
    # and an old album is deleted only after its surviving files have been moved out of it
    return deletion_operations + \
           album_rename_operations + \
           album_creation_operations + \
           movement_operations + \
           album_deletion_operations + \
           uploading_operations
