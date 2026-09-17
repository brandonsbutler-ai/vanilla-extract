# Look and feel

Recorded so it survives a rewrite of the window, and so a change to it is a
decision rather than a drift.

## Vanilla on black, particulars in orange

The window is warm black, piped in the cream of the ice cream it is named
after. Not a theme choice — the split is functional:

| Role | Colour | What it carries |
|---|---|---|
| Structure (the piping) | vanilla `#e3d2a8` | headings, rules, borders, the primary action |
| **Particulars** | orange `#ff9e3d` | the values the tool extracted: counts, discovered field names, the result line |
| Could not be read | magenta `#ff4fa3` | exceptions, and nothing else |
| Done | green `#7bd88f` | a completed step |

**Why the particulars get their own hue.** A person opening this window is
looking for the numbers — how many were read, how many were not, which fields
came out. Giving those one colour means they are found without reading
anything around them.

**Why magenta rather than amber.** Against cream piping the usual amber
warning is a near neighbour and disappears into it. Magenta is the one hue
on this screen that means only "this could not be read", which is the finding
the tool most needs a reader not to miss.

## Three surfaces

Everything used to sit on one flat ground, so there was no telling where one
field ended and the next began.

    BG    #0d0c0a   the page              warm black, not neutral grey
    CARD  #17150f   a panel on the page
    SUNK  #1f1c14   a row inside a panel

## Two faces

    SANS   everything a person wrote      headings, labels, explanations
    MONO   everything a machine produced  paths, filenames, counts, reasons

A path set in the same face as a sentence reads as prose and gets skimmed. In
a monospaced face it reads as a value and gets checked.

## Steps carry their state

Three numbered steps. Grey and numbered means not yet; cream and numbered means
this is where you are; green and ticked means done. The chip says which before
a word has been read.

## Not shared with anything else

This is Vanilla Extract's identity and no other product's. CobblerPy is a
different tool for a different buyer and deliberately looks nothing like this.
