# Public-domain books

[books.csv](books.csv) lists the 1,000 most downloaded English books on Project Gutenberg that Dripbook can sell as drips, because they are public domain in both the US and the EU.

A book is kept only if all of these hold:
- Gutenberg marks it as public domain in the US.
- Every author and translator died in 1955 or earlier (as of 2026). The EU protects a book for 70 years after its author's death.
- It's a whole book, not one volume of a set.

Books with an anonymous or "Various" author are left out, because there's no death to count from. When Gutenberg has several editions of a book, only the most downloaded is listed.

Two columns need a look:
- **`check_translator`** says `yes` when a book looks translated but Gutenberg names no translator. Open the Gutenberg page and check who translated it and when they died.
- **`category`** is one of Literature & classics, Philosophy & self-development, History & biography, Poetry & drama, Children's, Science & nature, Business & money, or Other. It comes from Gutenberg's own shelves, so a few books will be misfiled.

The download links follow from `gutenberg_id`. For example, `https://www.gutenberg.org/cache/epub/1342/pg1342-h.zip` is the HTML with images for ID 1342. Gutenberg blocks automated bulk downloads from its website, so fetch many books from a mirror.

## Rebuilding

The cut-off year moves forward each January:

```bash
python3 public-domain/build_list.py fetch --pages 62   # about an hour; can be stopped and resumed
python3 public-domain/build_list.py build              # offline, seconds
```

`fetch` saves Gutenberg's catalog to `cache/`, which git ignores. `build` only reads that copy, so you can change the rules and rebuild without fetching again. Delete `cache/` to fetch fresh download counts.

**This is a first filter, not legal clearance.** Before selling a book, open its Gutenberg edition and remove any modern introduction or editor's notes, plus Gutenberg's header, footer and name.

Offline tests:

```bash
python3 -m unittest discover -s public-domain
```
