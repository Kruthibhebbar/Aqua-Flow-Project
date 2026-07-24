/*=========================================================
 AquaFlow main.js
=========================================================*/

/* ==========================================
   Sticky Navbar
========================================== */

const navbar = document.querySelector(".navbar");

window.addEventListener("scroll", () => {

    if (window.scrollY > 80) {

        navbar.classList.add("shadow");

        navbar.style.padding = "12px 0";

        navbar.style.background = "rgba(255,255,255,.96)";

    } else {

        navbar.classList.remove("shadow");

        navbar.style.padding = "18px 0";

        navbar.style.background = "rgba(255,255,255,.85)";

    }

});

/* ==========================================
   Smooth Scroll
========================================== */

document.querySelectorAll('a[href^="#"]').forEach(anchor => {

    anchor.addEventListener("click", function (e) {

        e.preventDefault();

        const target = document.querySelector(this.getAttribute("href"));

        if (target) {

            target.scrollIntoView({

                behavior: "smooth"

            });

        }

    });

});

/* ==========================================
   Active Navigation
========================================== */

const sections = document.querySelectorAll("section");

const navLinks = document.querySelectorAll(".navbar-nav .nav-link");

window.addEventListener("scroll", () => {

    let current = "";

    sections.forEach(section => {

        const top = section.offsetTop - 180;

        const height = section.clientHeight;

        if (pageYOffset >= top) {

            current = section.getAttribute("id");

        }

    });

    navLinks.forEach(link => {

        link.classList.remove("active");

        if (link.getAttribute("href") == "#" + current) {

            link.classList.add("active");

        }

    });

});

/* ==========================================
   Counter Animation
========================================== */

const counters = document.querySelectorAll(".counter");

let counterStarted = false;

function startCounters() {

    if (counterStarted) return;

    const stats = document.querySelector(".stats");

    if (!stats) return;

    const trigger = stats.offsetTop - window.innerHeight + 120;

    if (window.scrollY > trigger) {

        counterStarted = true;

        counters.forEach(counter => {

            const target = +counter.dataset.target;

            let count = 0;

            const speed = target / 120;

            const update = () => {

                count += speed;

                if (count < target) {

                    counter.innerText = Math.floor(count);

                    requestAnimationFrame(update);

                } else {

                    counter.innerText = target;

                }

            };

            update();

        });

    }

}

window.addEventListener("scroll", startCounters);

/* ==========================================
   Reveal Animation
========================================== */

const reveals = document.querySelectorAll(

".feature-card,.service-card,.step-card,.testimonial-card,.stat-box,.about-image,.about-text,.mobile-image"

);

const observer = new IntersectionObserver(

(entries) => {

entries.forEach(entry => {

if(entry.isIntersecting){

entry.target.classList.add("fade-up");

}

});

},

{

threshold:.15

}

);

reveals.forEach(item=>{

observer.observe(item);

});

/* ==========================================
   Hero Parallax
========================================== */

window.addEventListener("mousemove",(e)=>{

const truck=document.querySelector(".hero-truck");

if(!truck) return;

let x=(window.innerWidth/2-e.pageX)/80;

let y=(window.innerHeight/2-e.pageY)/80;

truck.style.transform=

`translate(${x}px,${y}px)`;

});

/* ==========================================
   Floating Cards Animation
========================================== */

const cards=document.querySelectorAll(".floating-card");

cards.forEach((card,index)=>{

card.animate(

[

{

transform:"translateY(0px)"

},

{

transform:"translateY(-15px)"

},

{

transform:"translateY(0px)"

}

],

{

duration:3000+(index*500),

iterations:Infinity

}

);

});

/* ==========================================
   Back To Top Button
========================================== */

const topBtn=document.createElement("button");

topBtn.innerHTML='<i class="bi bi-arrow-up"></i>';

topBtn.id="topBtn";

document.body.appendChild(topBtn);

topBtn.style.cssText=`

position:fixed;

bottom:30px;

right:30px;

width:55px;

height:55px;

border:none;

border-radius:50%;

background:#2563EB;

color:white;

font-size:20px;

cursor:pointer;

display:none;

z-index:9999;

box-shadow:0 10px 25px rgba(0,0,0,.2);

transition:.3s;

`;

window.addEventListener("scroll",()=>{

if(window.scrollY>500){

topBtn.style.display="block";

}else{

topBtn.style.display="none";

}

});

topBtn.onclick=()=>{

window.scrollTo({

top:0,

behavior:"smooth"

});

};

/* ==========================================
   Loading Screen
========================================== */

window.addEventListener("load",()=>{

const loader=document.querySelector(".loader");

if(loader){

loader.style.opacity="0";

setTimeout(()=>{

loader.remove();

},500);

}

});

/* ==========================================
   Hover Tilt Effect
========================================== */

document.querySelectorAll(

".feature-card,.service-card,.testimonial-card"

).forEach(card=>{

card.addEventListener("mousemove",(e)=>{

const rect=card.getBoundingClientRect();

const x=e.clientX-rect.left;

const y=e.clientY-rect.top;

const rotateX=((y-rect.height/2)/18);

const rotateY=((rect.width/2-x)/18);

card.style.transform=

`perspective(800px)

rotateX(${rotateX}deg)

rotateY(${rotateY}deg)

translateY(-8px)`;

});

card.addEventListener("mouseleave",()=>{

card.style.transform="";

});

});

/* ==========================================
   Navbar Collapse (Mobile)
========================================== */

document.querySelectorAll(".navbar-nav a").forEach(link=>{

link.addEventListener("click",()=>{

const nav=document.querySelector(".navbar-collapse");

if(nav.classList.contains("show")){

bootstrap.Collapse.getInstance(nav).hide();

}

});

});

/* ==========================================
   Console Message
========================================== */

console.log(

"%c AquaFlow Loaded Successfully 🚛💧",

"background:#2563EB;color:white;padding:8px 15px;border-radius:6px;font-size:14px"

);
