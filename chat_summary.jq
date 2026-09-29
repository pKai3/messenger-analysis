def words:
  [.messages[]?.text // ""
    | [scan("[[:alnum:]'’]+")] | length
  ] | add // 0;

def person_stats($p):
  .messages as $m |
  [$m[] | select(.senderName == $p)] as $pm |
  {
    name: $p,
    messages: ($pm | length),
    text_messages: ([$pm[] | select(.type == "text")] | length),
    media_messages: ([$pm[] | select(.type == "media")] | length),
    words: ([$pm[].text // "" | [scan("[[:alnum:]'’]+")] | length] | add // 0),
    characters: ([$pm[].text // "" | length] | add // 0),
    reactions_given: ([$m[].reactions[]? | select(.actor == $p)] | length)
  };

def thread_summary:
  .messages as $m |
  {
    thread_name: .threadName,
    participants: .participants,
    total_messages: ($m | length),
    first_timestamp: ($m | map(.timestamp) | min // null),
    last_timestamp: ($m | map(.timestamp) | max // null),
    first_date: (if ($m | length) > 0 then (($m | map(.timestamp) | min) / 1000 | strftime("%Y-%m-%d")) else null end),
    last_date: (if ($m | length) > 0 then (($m | map(.timestamp) | max) / 1000 | strftime("%Y-%m-%d")) else null end),
    span_days: (if ($m | length) > 1 then ((($m | map(.timestamp) | max) - ($m | map(.timestamp) | min)) / 86400000) else 0 end),
    messages_per_day: (if ($m | length) > 1 then (($m | length) / (((($m | map(.timestamp) | max) - ($m | map(.timestamp) | min)) / 86400000) + 1)) else ($m | length) end),
    text_messages: ([$m[] | select(.type == "text")] | length),
    media_messages: ([$m[] | select(.type == "media")] | length),
    messages_with_media: ([$m[] | select((.media // [] | length) > 0)] | length),
    unsent_messages: ([$m[] | select(.isUnsent == true)] | length),
    total_reactions: ([$m[].reactions[]?] | length),
    total_words: ([$m[].text // "" | [scan("[[:alnum:]'’]+")] | length] | add // 0),
    total_characters: ([$m[].text // "" | length] | add // 0),
    people: [.participants[] as $p | person_stats($p)],
    reaction_types: ([$m[].reactions[]?.reaction] | sort | group_by(.) | map({reaction: .[0], count: length}) | sort_by(-.count)),
    message_types: ([$m[].type] | sort | group_by(.) | map({type: .[0], count: length}) | sort_by(-.count))
  };

map(thread_summary) as $threads |
{
  overall: {
    conversations: ($threads | length),
    total_messages: ([$threads[].total_messages] | add // 0),
    total_words: ([$threads[].total_words] | add // 0),
    total_media_messages: ([$threads[].media_messages] | add // 0),
    total_reactions: ([$threads[].total_reactions] | add // 0),
    total_unsent_messages: ([$threads[].unsent_messages] | add // 0),
    earliest_timestamp: ([$threads[].first_timestamp | select(. != null)] | min // null),
    latest_timestamp: ([$threads[].last_timestamp | select(. != null)] | max // null),
    earliest_date: ([$threads[].first_timestamp | select(. != null)] | if length > 0 then (min / 1000 | strftime("%Y-%m-%d")) else null end),
    latest_date: ([$threads[].last_timestamp | select(. != null)] | if length > 0 then (max / 1000 | strftime("%Y-%m-%d")) else null end),
    messages_by_sender: ([$threads[].people[]] | sort_by(.name) | group_by(.name) | map({name: .[0].name, messages: ([.[].messages] | add), words: ([.[].words] | add), media_messages: ([.[].media_messages] | add), reactions_given: ([.[].reactions_given] | add)}) | sort_by(-.messages)),
    largest_conversations: ($threads | map({thread_name, participants, total_messages, total_words, first_date, last_date, span_days, messages_per_day}) | sort_by(-.total_messages))
  },
  threads: ($threads | sort_by(-.total_messages))
}
