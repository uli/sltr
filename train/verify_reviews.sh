# runs reviews from review2html review directories through a verifier prompt
test -z "$MIX" && MIX="cat"

for i in $(ls "$1"/*_cot.txt| $MIX) ; do
	hash="$(basename "$i")"
	hash="${hash%%_*}"
	base="${i%_*}"
	out="$base"_verify${IDX}.txt
	test -e "$out" && continue
	echo -e '\nThe following is a review of this patch, including chain-of-thought, written by an LLM:\n\n```' >/tmp/$$
	sed -e 's,<think>,[CoT],g' -e 's,</think>,[end CoT],g' <"$base"_cot.txt >>/tmp/$$
	echo -e '\n```\n' >>/tmp/$$
	echo -e 'In the end the LLM delivered the following answer:\n\n```' >>/tmp/$$
	cat "$base"_answer.txt >>/tmp/$$
	echo -e '\n```\n' >>/tmp/$$
	cat prompts/post_verify_cot_1.txt >>/tmp/$$
	python review_commit.py -U 20 --review_post /tmp/$$ --out "$out" $REVIEW_ARGS $hash
	rm /tmp/$$
done
