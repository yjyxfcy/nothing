print (open --raw "data/access.log" | lines | where {|l| ($l =~ "GET") } | length)
open --raw data/access.log | lines | parse "{method} {path} {status}" | group-by method
