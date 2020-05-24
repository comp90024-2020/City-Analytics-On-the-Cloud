import sys

if len(sys.argv) < 2:
    print('Missing scenario argument', file=sys.stderr)
    sys.exit(1)

# Read input from stdin
stdinput = [line for line in sys.stdin]
ier = stdinput[0]
ieo = stdinput[1]
mapreduce_output = stdinput[2]

# f = open('f', 'w')
# f.writelines(stdinput)

if sys.argv[1] == 'sentiment':
    print(ier)
    print(ieo)
    print(mapreduce_output)
    pass
else:
    print('Bad scenario argument', file=sys.stderr)
    sys.exit(1)
