# Description
The project is aimed to create an automatic 
synchronization system between a local directory tree containing photos in 
jpeg-like format and an Immich database located on the server side.

# Architecture
## Main blocks
The project should consist of 3 main blocks: 
- Immich instance backend used as a blackbox via Immich API (located on a server node),
- SyncServer -- a server part of the project (located on the same node as 
the Immich backend),
- SyncClient -- a client part of the project (located on the client machine 
containing the directory tree with the photos locally).

## Interaction between the blocks
- Immich backend and SyncServer should communicate via Immich API;
- SyncServer and SyncClient should communicate via a custom REST API (described below).

## Blocks responsibilities
### SyncClient should:
- Calculate hash codes (SHA512 hash sums for file contents, collision probability is negligible) 
for all the photos from the local directory tree,
- Ask SyncServer to start the synchronization process,
- Ask SyncServer to collect hash codes from the Immich database,
- Poll SyncServer while it is computing the hash codes of the photos 
from the Immich database, 
- Ask SyncServer to command Immich to run several synchronization operations 
(the full list will follow below),
- Ask SyncServer to stop the synchronization process.

### SyncServer should:
- Generate a one-time API key to identify the current synchronization session 
by the SyncClient start synchronization request, and return it to SyncClient,
- Block all the synchronization requests from all the other SyncClients (if any),
- Request from Immich via the Immich API all the photos from its database one by one, and 
calculate hash codes for all of them,
- Pass on a command obtained from SyncClient to Immich (translate it from the SyncClient's 
REST API to the related Immich API command),
- Close the synchronization session by disabling the current session's one-time API key 
(right after that it should be able to start a new one from another SyncClient if any).

### SyncServer REST API endpoints
- `/v1/start-sync` -- Start synchronization request to SyncServer; 
SyncServer should generate the one-time API key and return it to SyncClient, from this 
moment and till it gets a request to stop the current synchronization session, SyncServer should 
refuse any other start synchronization requests;
- `/v1/collect` -- Collect hash codes request to SyncServer;
SyncServer should request from Immich via the Immich API all the photos from its database 
one by one, calculate hash codes for all of them and return them to the caller (note that it 
can take some time if the Immich database is large);
- Synchronization atomic operations that should be transferred to Immich API:
  - `/v1/upload` -- Upload a photo missing in the Immich database from the client node;
  - `/v1/delete` -- Delete an outdated photo from the Immich database;
  - `/v1/move` -- Move a photo from one album to another;
  - `/v1/create-album` -- Create a new album;
  - `/v1/delete-album` -- Delete an album;
  - `/v1/rename-album` -- Rename album;

- `/v1/stop-sync` -- Stop synchronization request to SyncServer;
SyncServer should disable the current session's one-time API key and start listen to other 
SyncClients' start synchronization requests (if any).
