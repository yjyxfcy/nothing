cat data/access.log | grep GET | wc -l
# @NU-PASSTHROUGH
# open --raw data/access.log | lines | parse "{method} {path} {status}" | group-by method
# @END-NU-PASSTHROUGH
