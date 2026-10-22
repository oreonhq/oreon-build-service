rule oreon_curl_or_wget_piped_to_shell {
  strings:
    $a = /curl[^\n]{0,160}\|\s*(\/usr\/bin\/|\/bin\/)?(ba|da|z)?sh/ nocase
    $b = /wget[^\n]{0,160}\|\s*(\/usr\/bin\/|\/bin\/)?(ba|da|z)?sh/ nocase
  condition:
    any of them
}

rule oreon_base64_decode_piped_to_shell {
  strings:
    $a = /base64[^\n]{0,40}-d[^\n]{0,80}\|\s*(\/usr\/bin\/|\/bin\/)?(ba|da|z)?sh/ nocase
  condition:
    $a
}
