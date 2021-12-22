
ts = {
    selected: null,
    numItems: 0
};

window.onload = (event) => {
    const searchInput = document.getElementById('search');

    const length = searchInput.value.length;
    searchInput.setSelectionRange(length, length);

    searchInput.addEventListener('keyup', (e) => {
        console.log("Key", e.key);
        if (e.key.length == 1 || e.key === 'Backspace') {
            console.log(searchInput.value);

            const encodedValue = encodeURIComponent(searchInput.value);
            fetch('/search?s=' + encodedValue).then(response => {
                clearResults();
                console.log(response);
                response.json().then(content => {
                    console.log(content);
                    for (const [i, element] of content.entries()) {
                        addResult(element.title, element.extract, element.url, i);
                    };
                    ts.selected = null;
                    ts.numItems = content.length;
                });
            });
        }
    });

    // Handle moving the selected item up and down
    document.addEventListener('keydown', (e) => {
        console.log("Key press", e);
        if (e.key == 'ArrowDown') {
            selectNextItem();
            e.preventDefault();
        } else if (e.key == 'ArrowUp') {
            selectPreviousItem();
            e.preventDefault();
        } else if (e.key == 'Enter') {
//            const form = document.getElementById('search-form');
//            form.submit();
//            event.preventDefault();
       }
    });

    // Handle pressing enter
    const form = document.getElementById('search-form');

    form.addEventListener("submit", event => {
        event.preventDefault();
        clickSelected();
    });

    searchInput.focus();
};


function selectNextItem() {
    if (ts.selected === null) {
        ts.selected = 0;
    } else if (ts.selected < ts.numItems -1) {
        ts.selected++;
    }

    updateSelected();
}


function clickSelected() {
    if (ts.selected !== null) {
        const selectedResult = document.getElementById(ts.selected.toString());
        selectedResult.click();
    }
}


function selectPreviousItem() {
    if (ts.selected === null) {
        return;
    } else if (ts.selected > 0) {
        ts.selected--;
    } else if (ts.selected == 0) {
        ts.selected = null;
    }

    updateSelected();
}


function updateSelected() {
    const results = document.querySelectorAll('.result');
    results.forEach(child => {
        child.classList.remove('selected');
    });

    if (ts.selected !== null) {
        const selectedResult = document.getElementById(ts.selected.toString());
        selectedResult.classList.add('selected');
    }
}


function clearResults() {
  const results = document.getElementById('results');
  results.innerHTML = '';
}


function addResult(title, extract, url, id) {
   const par = document.createElement("p");

   const titleText = createBoldedSpan(title);
   titleText.classList.add('title');
   const extractText = createBoldedSpan(extract);
   extractText.classList.add('extract');
   par.appendChild(titleText);

   separator = document.createTextNode(' - ')
   par.appendChild(separator);

   par.appendChild(extractText);

   const div = document.createElement("div");
   div.classList.add('result');
   div.id = id.toString();

   const urlPar = document.createElement("p");
   const urlText = document.createTextNode(url);
   urlPar.appendChild(urlText);
   urlPar.classList.add('url');
   div.appendChild(urlPar);
   div.appendChild(par);

   const link = document.createElement("a");
   link.appendChild(div);
   link.href = url;

   const results = document.getElementById('results');
   results.appendChild(link);
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