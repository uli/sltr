for i in "$@" ; do
	# overlong answer suggests the review script couldn't find the
	# </think> tag, which usually means the review ran out of context
	find "$i" -type f -name "*_answer.txt" -size +2000c|while read f ; do
		out="${f%_answer.txt}_verify.txt"
		test -e "$out" && continue
		echo "MANUAL_BOGUS overrun" > "$out"
	done

	# short answer indicates overrun while producing the final answer;
	# rare, but does happen
	find "$i" -type f -name "*_answer.txt" -size -80c|while read f ; do
		out="${f%_answer.txt}_verify.txt"
		test -e "$out" && continue
		echo "MANUAL_BOGUS overrun" > "$out"
	done

	# no CoT but an answer
	# happened once, the model simply didn't do anything in <think>
	fgrep -l "<no CoT>" "$i"/*_cot.txt|while read f ; do
		out="${f%_cot.txt}_verify.txt"
		test -e "$out" && continue
		echo "MANUAL_BOGUS no_cot" > "$out"
	done

	# Chinese CoT
	grep -El "(原|中)" "$i"/*_cot.txt|while read f ; do
		out="${f%_cot.txt}_verify.txt"
		test -e "$out" && continue
		echo "MANUAL_BOGUS chinese" > "$out"
	done

	# no or improperly formatted call for action
	for f in "$i"/*_answer.txt ; do
		out="${f%_answer.txt}_verify.txt"
		test -e "$out" && continue
		fgrep -q "**Action:** " "$f" || echo "MANUAL_BOGUS answer_format" > "$out"
	done

	# asterisks around the verdict
	grep -lEi "answer.*\*\*(apply|reject|check)\*\*" "$i"/*_cot.txt|while read f ; do
		out="${f%_cot.txt}_verify.txt"
		test -e "$out" && continue
		echo "MANUAL_BOGUS answer_format" > "$out"
	done
done
