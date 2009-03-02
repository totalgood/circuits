# Module:	__init__
# Date:		8th November 2008
# Author:	James Mills, prologic at shortcircuit dot net dot au

"""Circuits Tools

circuits.tools contains a standard set of tools for circuits. These
tools are installed as executables with a prefix of "circuits."
"""

def graph(x):
    s = []
    write = s.append

    d = 0
    i = 0
    done = False
    stack = []
    visited = set()
    children = list(x.components)
    while not done:
        if x not in visited:
            if d:
                write("%s%s\n" % (" " * d, "|"))
                write("%s%s%s\n" % (" " * d, "|-", x))
            else:
                write(" .%s\n" % x)

            if x.components:
                d += 1

            visited.add(x)

        if i < len(children):
            x = children[i]
            i += 1
            if x.components:
                stack.append((i, d, children))
                children = list(x.components)
                i = 0
        else:
            if stack:
                i, d, children = stack.pop()
            else:
                done = True

    return "".join(s)
