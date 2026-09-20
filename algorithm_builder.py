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

# Assuming the structure is a dict() where the key is a hash,
# and the value is a tree to the file relative to the photolab root.
# Item example:
#   'fef07ef14141b06370c1f37dd8ad5152f62bafe8f8e435f1ef181c7c82ca1a0618462a7716ba3d82618d92049528606eaf5b36ce48888e2791cd60781c967f2a': '2026/April/Misc/Джаз Sandia Quartet в Бабе-Яге/General/Personal/3 stars/0L5A0599_1.jpg'
def build_sync_algorithm(local_pairs, remote_pairs):
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
