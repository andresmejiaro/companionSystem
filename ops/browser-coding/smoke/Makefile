.PHONY: test clean

answer: answer.c
	$(CC) $(CFLAGS) -Wall -Wextra -Werror -std=c11 -o $@ $<

test: answer
	test "$$($(CURDIR)/answer)" = "43"

clean:
	$(RM) answer
