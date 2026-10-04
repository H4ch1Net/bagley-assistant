---
name: tidy-a-folder
description: Organise a messy workspace folder into sensible subfolders, with the user's agreement and every move revertible.
---

# Tidy a folder

1. `list_files` on the folder. Group the files by type, project or date, whichever gives the clearest structure.
2. Use `update_plan` to show the proposed folders and which files go where.
3. Ask for agreement with `ask_user` (options such as "Go ahead", "Only group by type", "Cancel"). Don't move anything before that.
4. Create folders with `make_directory` and move files with `move_file`. Never delete files while tidying.
5. Finish with a short summary of what moved where, and remind the user that each move can be reverted from the chat.
