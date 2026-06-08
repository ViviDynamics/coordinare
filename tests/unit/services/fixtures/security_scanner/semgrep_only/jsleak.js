// Fixture: a JS file semgrep (multi-language) would flag but bandit (Python-only) would not.
function render(userInput) {
  // CWE-79: DOM XSS — untrusted input written to innerHTML.
  document.getElementById("out").innerHTML = userInput;
}
