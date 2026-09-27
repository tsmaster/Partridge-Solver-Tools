import sys
#import path
import glob

import solution


log_filenames = ["../CppSolver/Solutions/soln_log.txt"]

for s in glob.glob("../Claude/Solutions/*.txt"):
    log_filenames.append(s)



def buffer_starts_at_line(line_no, lines):
    for i in range(0, 45):
        try:
            line = lines[line_no + i].strip()
        except IndexError:
            return False
        #print("line # %d len %d" % (line_no + i, len(line)))
        if len(line) != 45:
            return False
    return True

def get_raw_buffers(lines):
    for line_no in range(len(lines)):
        if buffer_starts_at_line(line_no, lines):
            yield lines[line_no:line_no+45]

def get_lineno_buffer_pairs(lines):
    for line_no in range(len(lines)):
        if buffer_starts_at_line(line_no, lines):
            yield (line_no, lines[line_no:line_no+45])

    
def make_solution_from_buffer(buffer):
    s = solution.make_solution_from_lines(buffer)
    return s
    

def main():
    solutions_count = 0
    print("Hello from splitlog!")

    buffer_list = []

    for fn in log_filenames:
        with open(fn) as f:
            lines = f.readlines()
            for line_no in range(len(lines)):
                if buffer_starts_at_line(line_no, lines):
                    #print("found solution buffer %d at %d" % (solutions_count, line_no))
                    solutions_count += 1
            buffer_list += list(get_raw_buffers(lines))
            print ("fn", fn, "count:", len(buffer_list))

    print("total count:", len(buffer_list))

    #process first 10 buffers
    for i in range(10):
        soln = solution.make_solution_from_lines(buffer_list[i])
        #print("made soln", soln)
        print("hash:", soln.get_hash())
    print()
    #process last 10 buffers
    for i in range(-10, 0):
        soln = solution.make_solution_from_lines(buffer_list[i])
        #print("made soln", soln)
        print("hash:", soln.get_hash())

    hash_set = set()
    for b in buffer_list:
        soln = solution.make_solution_from_lines(b)
        h = soln.get_hash()
        hash_set.add(h)
    hash_list = sorted(list(hash_set))

    # last hash
    print("unique hashes:", len(hash_list))
    print("last hash:", hash_list[0])

    soln_0 = solution.make_solution_from_lines(buffer_list[-1])
    soln_0.save_to_png("test_soln.png")


if __name__ == "__main__":
    main()
