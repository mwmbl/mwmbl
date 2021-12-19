

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
                    addResult(element.title, element.extract, element.url);
                })
            });
        });
    });
};


function clearResults() {
  const results = document.getElementById('results');
  results.innerHTML = '';
}


function addResult(title, extract, url) {
   const par = document.createElement("p");

   const link = document.createElement("a");
   const titleText = createBoldedSpan(title);
   titleText.classList.add('title');
   const extractText = createBoldedSpan(extract);
   extractText.classList.add('extract');
   link.appendChild(titleText);

   separator = document.createTextNode(' - ')
   link.appendChild(separator);

   link.appendChild(extractText);
   link.href = url;

   par.appendChild(link);

   const results = document.getElementById('results');
   results.appendChild(par);
}

function createBoldedSpan(title) {
    span = document.createElement('span');
    title.forEach(element => {
        text = document.createTextNode(element.value);
        if (element.is_bold) {
            b = document.createElement('span');
            b.classList.add('term');
            b.appendChild(text);
            span.appendChild(b);
        } else {
            span.appendChild(text);
        }
    });
    return span;
}