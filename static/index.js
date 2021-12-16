

window.onload = (event) => {
    const searchInput = document.getElementById('search');

    const length = searchInput.value.length;
    searchInput.setSelectionRange(length, length);

    searchInput.addEventListener('keyup', (e) => {
        console.log(searchInput.value);

        const encodedValue = encodeURIComponent(searchInput.value);
        fetch('/search?s=' + encodedValue).then(response => {
            clearResults();
            console.log(response);
            response.json().then(content => {
                console.log(content);
                content.forEach(element => {
                    addResult(element.title, element.url);
                })
            });
        });
    });
};


function clearResults() {
  const results = document.getElementById('results');
  results.innerHTML = '';
}


function addResult(title, url) {
   const par = document.createElement("p");

   const link = document.createElement("a");
   const linkText = document.createTextNode(title);
   link.appendChild(linkText);
   link.href = url;

   par.appendChild(link);

   const results = document.getElementById('results');
   results.appendChild(par);
}