Safe creating and editing of files (artifacts):

1. Before writing a file, read its path with `file_read` (or `editor` with `view`). If the read says the file does not exist, you are creating a new one.
2. Create a new file with `editor` `create`. It never overwrites an existing file.
3. To change a file you were asked to change, make the smallest edit with `editor` `str_replace` (the old text must be unique in the file) or `insert`. Do not rewrite a whole existing file with `file_write`.
4. Do not overwrite a file you were not asked to change. Write a new version under a new name instead (for example `notes.v2.md`) with `editor` `create`.
5. After every write, read the file again with `file_read` and check that it contains what you intended.
6. If a write is refused (the path is outside where you may write, it is protected, or a person declined the request), stop and say so. Do not try another tool or another path to get around the refusal.
7. In your report, separate what you checked (you read it back) from what you did not check.
